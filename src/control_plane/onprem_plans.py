"""On-prem plans and forward DB sync. The cloud engine never consumes these records."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from control_plane.adapters.client import AdapterClient, AdapterError, AdapterUnreachable
from control_plane.api.schemas import (
    CreateWorkflow,
    EventPage,
    HoldView,
    ResourceView,
    StageView,
    WorkflowCommand,
    WorkflowDetail,
)
from control_plane.clock import Clock
from control_plane.domain.models import (
    Model,
    ResourceKind,
    StageState,
    StageStatus,
    User,
    Workflow,
    WorkflowEvent,
    WorkflowStatus,
)
from control_plane.domain.stages import PLAN
from control_plane.errors import ApiError
from control_plane.onprem_database import OnpremDatabase, migration_request
from control_plane.store.table import Table, VersionConflict

ONPREM_PREFIX = "wf-onprem-"


class PlanRecord(Model):
    workflow: Workflow
    events: list[WorkflowEvent]
    database_request: dict[str, object] | None = None
    migration_id: str | None = None
    pending: Literal["CREATE", "PREPARE", "START_SYNC"] | None = None
    expected_revision: int | None = None
    operation_id: str | None = None

    def detail(self) -> WorkflowDetail:
        stages = [StageState(index=i, kind=stage.kind) for i, stage in enumerate(PLAN)]
        stages[0].detail = "온프렘 실행 환경과 데이터 대상 연결을 기다리는 계획입니다."
        return WorkflowDetail(
            workflow=self.workflow,
            allowed_commands=["ABORT"] if self.workflow.status == WorkflowStatus.PLANNED else [],
            stages=[StageView.of(s) for s in stages],
            hold=HoldView(
                is_live=False, desired="UNKNOWN", generation=0, held_since=None, actors=[]
            ),
            resources=[
                ResourceView(
                    kind=ResourceKind.DATABASE,
                    migration_id=None,
                    lifecycle=None,
                    health=None,
                    route=None,
                    phase="PLANNED",
                    target_writes_possible=False,
                    readiness=None,
                    observed_at=None,
                    details={"profile": self.database_request["profile"]},
                )
            ]
            if self.database_request is not None
            else [],
            traffic=[],
        )

    def page(self, after: str | None, limit: int) -> EventPage:
        if after is not None and (not after.isascii() or not after.isdecimal() or len(after) > 20):
            raise ApiError(422, "INVALID_CURSOR", "expected a numeric event cursor")
        events = [event for event in self.events if after is None or int(event.cursor) > int(after)]
        page = events[:limit]
        return EventPage(events=page, next_cursor=page[-1].cursor if len(events) > limit else None)


class OnpremPlans:
    def __init__(
        self,
        table: Table,
        clock: Clock,
        application: str,
        sources: set[str],
        *,
        database: AdapterClient | None = None,
        endpoints: OnpremDatabase | None = None,
    ) -> None:
        self.table = table
        self.clock = clock
        self.application = application
        self.sources = sources
        self.database = database
        self.endpoints = endpoints

    def executable(self, plan: PlanRecord) -> bool:
        wf = plan.workflow
        return bool(
            self.database is not None
            and self.endpoints is not None
            and (wf.source_env, wf.target_env) == ("aws", "onprem")
            and plan.database_request == self.endpoints.request(wf.id)
        )

    def partition(self, owner: str) -> str:
        return f"ONPREM_PLANS#{self.application}#{owner}"

    async def create(self, body: CreateWorkflow, user: User) -> PlanRecord:
        pair = {body.source_env, body.target_env}
        if len(pair) != 2 or "onprem" not in pair or not (pair - {"onprem"}) <= self.sources:
            raise ApiError(422, "INVALID_ONPREM_DIRECTION", "select a cloud and the on-prem node")
        if body.resources and (
            (body.source_env, body.target_env) != ("aws", "onprem")
            or set(body.resources) != {ResourceKind.DATABASE}
        ):
            raise ApiError(
                422,
                "ONPREM_ENDPOINTS_NOT_CONFIGURED",
                "only AWS to on-prem database inputs are supported",
            )
        if body.target_configuration is not None:
            raise ApiError(
                409, "TARGET_SIZING_UNAVAILABLE", "on-prem plans have no execution backend bound"
            )
        now = self.clock.now()
        endpoints = body.resources.get(ResourceKind.DATABASE)
        if endpoints is None and (body.source_env, body.target_env) == ("aws", "onprem"):
            endpoints = self.endpoints
        workflow = Workflow(
            id=ONPREM_PREFIX + uuid4().hex,
            application=self.application,
            source_env=body.source_env,
            target_env=body.target_env,
            status=WorkflowStatus.PLANNED,
            created_by=user.login,
            created_at=now,
            version=1,
        )
        plan = PlanRecord(
            workflow=workflow,
            database_request=migration_request(workflow.id, endpoints) if endpoints else None,
            events=[
                WorkflowEvent(
                    cursor="1",
                    at=now,
                    type="WORKFLOW_CREATED",
                    actor=user.login,
                    message=f"{body.source_env} to {body.target_env}",
                )
            ],
        )
        if (
            self.endpoints
            and endpoints
            and (
                endpoints.source == self.endpoints.source
                and endpoints.target == self.endpoints.target
            )
        ):
            plan.database_request = self.endpoints.request(workflow.id)
        await self.table.put(
            self.partition(user.id), workflow.id, plan.model_dump_json(), expected_version=0
        )
        return plan

    async def get(self, workflow_id: str, owner: str) -> PlanRecord:
        row = await self.table.get(self.partition(owner), workflow_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "on-prem plan not found")
        return PlanRecord.model_validate_json(row.body)

    async def list(self, owner: str) -> list[Workflow]:
        found: list[Workflow] = []
        cursor = None
        while True:
            page = await self.table.query(self.partition(owner), ONPREM_PREFIX, after=cursor)
            found.extend(PlanRecord.model_validate_json(row.body).workflow for row in page.items)
            if page.next_sk is None:
                return found
            cursor = page.next_sk

    async def describe(self, plan: PlanRecord) -> WorkflowDetail:
        detail = plan.detail()
        if not self.executable(plan) or plan.workflow.status == WorkflowStatus.ABORTED:
            return detail
        detail.allowed_commands = []
        stage = detail.stages[plan.workflow.current_stage]
        stage.detail = "AWS 원본과 온프렘 DB의 준비를 승인하세요."
        if plan.workflow.status == WorkflowStatus.PLANNED:
            detail.allowed_commands = ["APPROVE", "ABORT"]
        elif plan.pending:
            detail.allowed_commands = ["APPROVE"]
            stage.detail = f"{plan.pending} 요청 결과를 확인하려면 다시 승인하세요."
            stage.status = StageStatus.RUNNING
        if not plan.migration_id:
            return detail
        assert self.database is not None
        resource = detail.resources[0]
        resource.migration_id = plan.migration_id
        resource.active_operation_id = plan.operation_id
        try:
            view = await self.database.migration(plan.migration_id)
            op = await self.database.operation(plan.operation_id) if plan.operation_id else None
        except (AdapterError, AdapterUnreachable):
            stage.detail = "DB 어댑터 상태를 조회할 수 없습니다."
            return detail
        resource.lifecycle = resource.phase = view.lifecycle
        resource.health, resource.route = view.health, view.route
        resource.target_writes_possible = view.target_writes_possible
        resource.readiness = view.raw.get("readiness")
        if plan.pending:
            return detail
        stage.status = StageStatus.RUNNING
        stage.detail = f"온프렘 DB 상태: {view.lifecycle}"
        if op and op.status == "FAILED":
            stage.status = StageStatus.HALTED
            stage.detail = f"DB 작업 실패: {(op.error or {}).get('code', 'UNKNOWN')}"
            detail.workflow = detail.workflow.model_copy(update={"status": WorkflowStatus.HALTED})
        elif (
            plan.workflow.current_stage == 0
            and op is not None
            and op.status == "SUCCEEDED"
            and view.lifecycle == "PREPARED"
            and not view.active_operation_id
            and "START_SYNC" in view.allowed_actions
        ):
            stage.status = StageStatus.PASSED
            stage.detail = "DB 준비 완료. 다음 승인으로 RDS → 온프렘 복제를 시작합니다."
            detail.allowed_commands = ["APPROVE"]
        elif plan.workflow.current_stage == 1:
            detail.stages[0].status = StageStatus.PASSED
            detail.stages[0].detail = "온프렘 DB 준비 완료"
            if view.lifecycle == "READY":
                stage.status = StageStatus.PASSED
                stage.detail = "DB 동기화 완료. 쓰기 전환은 실행하지 않았습니다."
        return detail

    async def save(self, plan: PlanRecord, user: User, event: str) -> None:
        wf = plan.workflow
        version = wf.version
        wf.version += 1
        plan.events.append(
            WorkflowEvent(
                cursor=str(wf.version),
                at=self.clock.now(),
                type=event,
                actor=user.login,
                message=event,
            )
        )
        try:
            await self.table.put(
                self.partition(user.id), wf.id, plan.model_dump_json(), expected_version=version
            )
        except VersionConflict as error:
            raise ApiError(409, "VERSION_CONFLICT", "the on-prem plan has changed") from error

    async def approve(self, plan: PlanRecord, user: User) -> PlanRecord:
        if not self.executable(plan):
            raise ApiError(
                409, "ONPREM_EXECUTION_NOT_CONFIGURED", "configure the AWS to on-prem DB pair"
            )
        assert self.database is not None and plan.database_request is not None
        wf = plan.workflow
        try:
            if not plan.pending:
                if wf.status == WorkflowStatus.PLANNED:
                    wf.status = WorkflowStatus.RUNNING
                    plan.pending = "CREATE"
                elif (
                    wf.status == WorkflowStatus.RUNNING
                    and wf.current_stage == 0
                    and plan.migration_id
                ):
                    view = await self.database.migration(plan.migration_id)
                    if (
                        not plan.operation_id
                        or (await self.database.operation(plan.operation_id)).status != "SUCCEEDED"
                        or view.lifecycle != "PREPARED"
                        or view.active_operation_id
                        or "START_SYNC" not in view.allowed_actions
                    ):
                        raise ApiError(
                            409, "DB_NOT_PREPARED", "wait for the DB adapter to finish preparing"
                        )
                    wf.current_stage = 1
                    plan.pending, plan.expected_revision = "START_SYNC", view.revision
                else:
                    raise ApiError(409, "INVALID_STATE", "no forward DB step is available")
                # Win against ABORT and competing approvals before contacting the adapter.
                await self.save(plan, user, f"{plan.pending}_REQUESTED")
            if plan.pending == "CREATE":
                created = await self.database.create_migration(
                    f"{wf.id}:create", plan.database_request
                )
                plan.migration_id = created.migration_id
                plan.pending, plan.expected_revision = "PREPARE", created.revision
                await self.save(plan, user, "DB_REGISTERED")
            assert plan.migration_id and plan.pending and plan.expected_revision is not None
            action = plan.pending
            plan.operation_id = await self.database.act(
                plan.migration_id,
                f"{wf.id}:{action}",
                {"action": action, "expectedRevision": plan.expected_revision},
            )
            plan.pending, plan.expected_revision = None, None
            await self.save(plan, user, f"{action}_ACCEPTED")
        except AdapterError as error:
            if plan.pending == "CREATE" and error.code in {
                "BINDING_NOT_READY",
                "SOURCE_MISMATCH",
                "BINDING_REQUIRED",
                "RESOURCE_MISMATCH",
            }:
                # The adapter rejected the source before allocating this migration.
                wf.status = WorkflowStatus.PLANNED
                plan.pending = None
                await self.save(plan, user, error.code)
            raise ApiError(error.status, error.code, error.message, error.blockers) from error
        except AdapterUnreachable as error:
            raise ApiError(
                503, "DB_ADAPTER_UNREACHABLE", "reload and retry the same DB step"
            ) from error
        return plan

    async def command(self, workflow_id: str, body: WorkflowCommand, user: User) -> PlanRecord:
        plan = await self.get(workflow_id, user.id)
        wf = plan.workflow
        if wf.version != body.expected_version:
            raise ApiError(409, "VERSION_CONFLICT", "the on-prem plan has changed")
        if body.command == "APPROVE":
            return await self.approve(plan, user)
        if wf.status != WorkflowStatus.PLANNED:
            raise ApiError(409, "INVALID_STATE", "the on-prem plan is closed")
        if body.command != "ABORT":
            raise ApiError(
                409,
                "ONPREM_EXECUTION_NOT_CONFIGURED",
                "connect the on-prem execution backend first",
            )
        wf.status = WorkflowStatus.ABORTED
        wf.finished_at = self.clock.now()
        await self.save(plan, user, "ABORT")
        return plan
