"""Record every forward, compensation and abort traffic operation with its external ID."""

from control_plane.domain.models import TrafficRecord
from control_plane.engine.context import StageContext
from control_plane.traffic import AppliedWeights, TrafficError


async def set_traffic(ctx: StageContext, percent: int, purpose: str) -> AppliedWeights:
    identity = f"{ctx.stage.kind.value}-{purpose}-{percent}-{ctx.workflow.retry_epoch}"
    record = TrafficRecord(
        operation_id=identity,
        at=ctx.now,
        requested={ctx.workflow.source_env: 100 - percent, ctx.workflow.target_env: percent},
    )
    await ctx.services.repo.save_traffic(ctx.workflow.id, record)
    try:
        applied = await ctx.services.traffic.set_split(
            source_pool=ctx.source_env.traffic_pool,
            target_pool=ctx.target_env.traffic_pool,
            target_percent=percent,
            migration_id=ctx.workflow.id,
            transition_id=identity,
        )
    except TrafficError as error:
        record.adapter_operation_id = error.operation.get("operationId")
        record.operation_status = error.operation.get("status", "UNCONFIRMED")
        record.started_at = error.operation.get("startedAt")
        record.finished_at = error.operation.get("finishedAt")
        await ctx.services.repo.save_traffic(ctx.workflow.id, record)
        raise
    record.applied, record.effective_percent = applied.weights, applied.effective_percent
    record.verified = record.config_verified = True
    record.adapter_operation_id, record.operation_status = (
        applied.operation_id,
        applied.operation_status,
    )
    record.started_at, record.finished_at = applied.started_at, applied.finished_at
    await ctx.services.repo.save_traffic(ctx.workflow.id, record)
    return applied
