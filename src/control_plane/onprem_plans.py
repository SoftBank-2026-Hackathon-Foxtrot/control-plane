"""On-prem migration planning. Its records are never consumed by the cloud workflow engine."""

from __future__ import annotations

from uuid import uuid4

from control_plane.api.schemas import (
    CreateWorkflow,
    EventPage,
    HoldView,
    StageView,
    WorkflowCommand,
    WorkflowDetail,
)
from control_plane.clock import Clock
from control_plane.domain.models import (
    Model,
    StageState,
    User,
    Workflow,
    WorkflowEvent,
    WorkflowStatus,
)
from control_plane.domain.stages import PLAN
from control_plane.errors import ApiError
from control_plane.store.table import Table, VersionConflict

ONPREM_PREFIX = "wf-onprem-"


class PlanRecord(Model):
    workflow: Workflow
    events: list[WorkflowEvent]

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
            resources=[],
            traffic=[],
        )

    def page(self, after: str | None, limit: int) -> EventPage:
        events = [event for event in self.events if after is None or event.cursor > after]
        page = events[:limit]
        return EventPage(events=page, next_cursor=page[-1].cursor if len(events) > limit else None)


class OnpremPlans:
    def __init__(self, table: Table, clock: Clock, application: str, sources: set[str]) -> None:
        self.table = table
        self.clock = clock
        self.application = application
        self.sources = sources

    def partition(self, owner: str) -> str:
        return f"ONPREM_PLANS#{self.application}#{owner}"

    async def create(self, body: CreateWorkflow, user: User) -> PlanRecord:
        pair = {body.source_env, body.target_env}
        if len(pair) != 2 or "onprem" not in pair or not (pair - {"onprem"}) <= self.sources:
            raise ApiError(422, "INVALID_ONPREM_DIRECTION", "select a cloud and the on-prem node")
        if body.resources:
            raise ApiError(422, "ONPREM_ENDPOINTS_NOT_CONFIGURED", "no execution backend is bound")
        now = self.clock.now()
        workflow = Workflow(
            id=ONPREM_PREFIX + uuid4().hex,
            application=self.application,
            source_env=body.source_env,
            target_env=body.target_env,
            created_by=user.login,
            created_at=now,
            version=1,
        )
        plan = PlanRecord(
            workflow=workflow,
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

    async def command(self, workflow_id: str, body: WorkflowCommand, user: User) -> PlanRecord:
        plan = await self.get(workflow_id, user.id)
        wf = plan.workflow
        if wf.version != body.expected_version:
            raise ApiError(409, "VERSION_CONFLICT", "the on-prem plan has changed")
        if wf.status != WorkflowStatus.PLANNED:
            raise ApiError(409, "INVALID_STATE", "the on-prem plan is closed")
        if body.command != "ABORT":
            raise ApiError(
                409,
                "ONPREM_EXECUTION_NOT_CONFIGURED",
                "connect the on-prem execution backend first",
            )
        now = self.clock.now()
        wf.status = WorkflowStatus.ABORTED
        wf.version += 1
        wf.finished_at = now
        plan.events.append(
            WorkflowEvent(
                cursor="2", at=now, type="ABORT", actor=user.login, message="On-prem plan closed"
            )
        )
        try:
            await self.table.put(
                self.partition(user.id),
                wf.id,
                plan.model_dump_json(),
                expected_version=body.expected_version,
            )
        except VersionConflict as error:
            raise ApiError(409, "VERSION_CONFLICT", "the on-prem plan has changed") from error
        return plan
