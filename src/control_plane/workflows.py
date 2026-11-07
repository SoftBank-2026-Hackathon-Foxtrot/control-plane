"""What a person may ask of a workflow. Each command checks the state it is allowed from."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from control_plane.adapters.client import AdapterError, AdapterUnreachable
from control_plane.adapters.profiles import PROFILES
from control_plane.bindings import BindingService
from control_plane.clock import Clock
from control_plane.domain.models import (
    EndpointPair,
    Halt,
    PreparedTargetConfiguration,
    ResourceKind,
    RollbackRequest,
    StageKind,
    StageState,
    Workflow,
    WorkflowEvent,
    WorkflowStatus,
)
from control_plane.domain.stages import CUTOVER_ORDER, PLAN
from control_plane.errors import ApiError
from control_plane.seed import Seed
from control_plane.store.repository import ActiveWorkflowExists, Repository
from control_plane.store.table import VersionConflict

Clients = dict[ResourceKind, Any]

ALLOWED_COMMANDS: dict[WorkflowStatus, tuple[str, ...]] = {
    WorkflowStatus.PLANNED: ("RESUME", "ABORT"),
    WorkflowStatus.RUNNING: ("HALT",),
    WorkflowStatus.WAITING_APPROVAL: ("APPROVE", "ABORT"),
    WorkflowStatus.CUTOVER_COMPLETE: ("ROLLBACK", "FINALIZE"),
    WorkflowStatus.HALTED: ("RESUME", "RECONCILE", "ROLLBACK_RESOURCE", "ABORT"),
    WorkflowStatus.ROLLING_BACK: (),
    WorkflowStatus.ABORTING: (),
    WorkflowStatus.COMPLETED: (),
    WorkflowStatus.ABORTED: (),
    WorkflowStatus.ROLLED_BACK: (),
}


def commands_for(workflow: Workflow) -> tuple[str, ...]:
    if workflow.status is WorkflowStatus.HALTED and workflow.rollback_all:
        return ("ROLLBACK", "RECONCILE")
    if workflow.status is WorkflowStatus.WAITING_APPROVAL:
        # Legacy plans are normalized by the engine. Never label a finalization choice as
        # another cutover approval while a client reads the state between ticks.
        stage = PLAN[workflow.current_stage].kind
        if stage is StageKind.PREPARE:
            return ("HALT",)
        if stage is StageKind.FINALIZE:
            return ("ROLLBACK", "FINALIZE")
    return ALLOWED_COMMANDS[workflow.status]


@dataclass(frozen=True)
class CommandInput:
    command: str
    expected_version: int
    resource: ResourceKind | None = None
    mode: str = "normal"
    confirm_target_failure: bool = False


class WorkflowService:
    def __init__(self, repo: Repository, seed: Seed, clock: Clock, adapters: Clients) -> None:
        self.repo = repo
        self.seed = seed
        self.clock = clock
        self.adapters = adapters
        self.bindings = BindingService(repo, seed, adapters)

    async def create(
        self,
        *,
        source_env: str,
        target_env: str,
        actor: str,
        actor_id: str | None = None,
        resources: dict[ResourceKind, EndpointPair] | None = None,
        target_configuration: PreparedTargetConfiguration | None = None,
    ) -> Workflow:
        active = await self.repo.active_workflow_id()
        if active:
            raise ApiError(409, "WORKFLOW_ACTIVE", "another migration has not finished", [active])
        resources = resources or {}
        if ResourceKind.OBJECT_STORAGE in resources:
            raise ApiError(422, "UNSUPPORTED_RESOURCE", "object migration keeps its existing refs")
        for env in (source_env, target_env):
            if env not in self.seed.environments:
                raise ApiError(422, "UNKNOWN_ENVIRONMENT", f"{env} is not in the seed")
        if source_env == target_env:
            raise ApiError(422, "SAME_ENVIRONMENT", "source and target are the same environment")
        await self._check_capabilities()
        workflow = Workflow(
            id=f"wf-{secrets.token_hex(4)}",
            application=self.seed.application,
            source_env=source_env,
            target_env=target_env,
            created_by=actor,
            created_by_id=actor_id,
            created_at=self.clock.now(),
            target_configuration=target_configuration,
        )
        plans = []
        for kind in (ResourceKind.DATABASE, ResourceKind.REDIS):
            plan = await self.bindings.plan(workflow, kind, resources.get(kind))
            if plan is not None:
                plans.append(plan)
        try:
            await self.repo.create_workflow(
                workflow,
                stages=(StageState(index=i, kind=stage.kind) for i, stage in enumerate(PLAN)),
                resources=plans,
            )
        except ActiveWorkflowExists as error:
            raise ApiError(
                409,
                "WORKFLOW_ACTIVE",
                "another migration has not finished",
                [error.workflow_id],
            ) from error
        await self._event(workflow, "WORKFLOW_CREATED", actor, f"{source_env} to {target_env}")
        saved = await self.repo.workflow(workflow.id)
        assert saved is not None
        return saved

    async def _check_capabilities(self) -> None:
        """Refuse a plan up front if an adapter does not offer what the seed relies on."""
        for kind in self.seed.managed:
            profile = PROFILES[kind]
            try:
                capabilities = await self.adapters[kind].capabilities()
            except (AdapterUnreachable, AdapterError) as error:
                raise ApiError(503, "ADAPTER_UNAVAILABLE", f"{kind.value}: {error}") from error
            offered = {entry.get("profile") for entry in capabilities.get("profiles", [])}
            selected = profile.selected_profile(self.seed.resources.of(kind))
            if selected not in offered:
                raise ApiError(
                    422,
                    "UNSUPPORTED_PROFILE",
                    f"{kind.value} does not offer {selected}",
                    sorted(str(name) for name in offered),
                )
            required_mode = profile.required_rollback_mode(self.seed.resources.of(kind))
            if required_mode is not None:
                supported = any(
                    entry.get("profile") == selected
                    and entry.get("kind") == kind.value
                    and required_mode in (entry.get("rollbackModes") or [])
                    and "ROLLBACK" in (entry.get("actions") or [])
                    for entry in capabilities.get("profiles", [])
                )
                if not supported:
                    raise ApiError(
                        422,
                        "UNSUPPORTED_ROLLBACK_MODE",
                        f"{selected} must advertise {required_mode} and ROLLBACK",
                    )

    async def apply(self, workflow_id: str, command: CommandInput, *, actor: str) -> Workflow:
        workflow = await self.repo.workflow(workflow_id)
        if workflow is None:
            raise ApiError(404, "NOT_FOUND", f"no workflow {workflow_id}")
        if workflow.version != command.expected_version:
            raise ApiError(
                409, "VERSION_CONFLICT", f"the workflow is at version {workflow.version}"
            )
        if command.command not in commands_for(workflow):
            raise ApiError(
                409,
                "INVALID_STATE",
                f"{command.command} is not allowed while {workflow.status.value}",
                list(commands_for(workflow)),
            )
        if command.resource is not None and command.resource not in self.seed.managed:
            raise ApiError(
                422,
                "RESOURCE_NOT_MANAGED",
                f"{command.resource.value} is not managed by this application",
                [kind.value for kind in self.seed.managed],
            )
        handler = getattr(self, f"_{command.command.lower()}")
        updated: Workflow = handler(workflow, command, actor)
        try:
            saved = await self.repo.save_workflow(updated)
        except VersionConflict as error:
            raise ApiError(
                409, "VERSION_CONFLICT", "the workflow changed, check its state again"
            ) from error
        await self._event(workflow, command.command, actor, command.resource or "")
        return saved

    @staticmethod
    def _approve(workflow: Workflow, _: CommandInput, __: str) -> Workflow:
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.RUNNING,
                "approved_through": workflow.current_stage,
                "retry_epoch": workflow.retry_epoch + 1,
            }
        )

    @staticmethod
    def _finalize(workflow: Workflow, _: CommandInput, __: str) -> Workflow:
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.RUNNING,
                "finalize_requested": True,
                "retry_epoch": workflow.retry_epoch + 1,
            }
        )

    def _rollback(self, workflow: Workflow, _: CommandInput, actor: str) -> Workflow:
        if workflow.reconcile_requested:
            raise ApiError(409, "RECONCILE_RUNNING", "wait for the reconcile to finish")
        pending = [
            kind
            for kind in reversed(CUTOVER_ORDER)
            if kind in self.seed.managed and kind not in workflow.rolled_back
        ]
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.ROLLING_BACK,
                "rollback_all": True,
                "rollback": RollbackRequest(resource=pending[0], requested_by=actor)
                if pending
                else None,
                "halt": None,
                "retry_epoch": workflow.retry_epoch + 1,
            }
        )

    def _halt(self, workflow: Workflow, _: CommandInput, actor: str) -> Workflow:
        # The running command keeps its record. Resuming polls it instead of sending another.
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.HALTED,
                "halt": Halt(
                    code="HALTED_BY_PERSON",
                    message=f"halted by {actor}",
                    at=self.clock.now(),
                ),
            }
        )

    @staticmethod
    def _resume(workflow: Workflow, _: CommandInput, __: str) -> Workflow:
        if workflow.rolled_back:
            raise ApiError(
                409,
                "NEW_PLAN_REQUIRED",
                "a resource returned to its source: abort this workflow and start a new one",
                [kind.value for kind in workflow.rolled_back],
            )
        if workflow.reconcile_requested:
            raise ApiError(409, "RECONCILE_RUNNING", "wait for the reconcile to finish")
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.RUNNING,
                "halt": None,
                "retry_epoch": workflow.retry_epoch + 1,
            }
        )

    @staticmethod
    def _reconcile(workflow: Workflow, _: CommandInput, __: str) -> Workflow:
        return workflow.model_copy(
            update={
                "reconcile_requested": True,
                "reconcile_count": workflow.reconcile_count + 1,
            }
        )

    @staticmethod
    def _rollback_resource(workflow: Workflow, command: CommandInput, actor: str) -> Workflow:
        if command.resource is None:
            raise ApiError(400, "INVALID_REQUEST", "resourceId is required")
        if command.mode not in {"normal", "failure"}:
            raise ApiError(400, "INVALID_REQUEST", "mode must be normal or failure")
        if command.mode == "failure":
            if command.resource is not ResourceKind.REDIS:
                raise ApiError(422, "UNSUPPORTED_MODE", "only Redis has a failure mode")
            if not command.confirm_target_failure:
                raise ApiError(
                    422,
                    "CONFIRMATION_REQUIRED",
                    "failure mode can lose data: confirmTargetFailure is required",
                )
        return workflow.model_copy(
            update={
                "status": WorkflowStatus.ROLLING_BACK,
                "retry_epoch": workflow.retry_epoch + 1,
                "rollback": RollbackRequest(
                    resource=command.resource,
                    mode=command.mode,
                    requested_by=actor,
                    target_failure_confirmed=command.confirm_target_failure,
                ),
            }
        )

    @staticmethod
    def _abort(workflow: Workflow, _: CommandInput, __: str) -> Workflow:
        return workflow.model_copy(
            update={"status": WorkflowStatus.ABORTING, "retry_epoch": workflow.retry_epoch + 1}
        )

    async def _event(self, workflow: Workflow, type_: str, actor: str, message: str) -> None:
        await self.repo.add_event(
            workflow.id,
            WorkflowEvent(
                at=self.clock.now(),
                type=type_,
                actor=actor,
                stage=workflow.current_stage,
                message=str(message),
            ),
        )
