from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request

from control_plane.ai.context import DATA_SCOPE
from control_plane.ai.schemas import AnalysisRequest, ChatRequest, RecommendationRequest
from control_plane.ai.service import AIService
from control_plane.api.dependencies import CheckedSession, Context, CurrentSession
from control_plane.api.idempotency import once

router = APIRouter(prefix="/v1/ai", tags=["AI"])
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def service(request: Request) -> AIService:
    value: AIService = request.app.state.ai_service
    return value


@router.get("/status")
async def status(request: Request, context: Context, _: CurrentSession) -> dict[str, Any]:
    return {
        "configured": service(request).model.configured,
        "model": context.settings.ai_model,
        "prometheusConfigured": bool(context.settings.prometheus_url),
        "tempoConfigured": bool(context.settings.tempo_url),
        "retentionDays": context.settings.ai_retention_days,
        "sizingEnvironments": ["aws", "gcp"],
        "dataScope": DATA_SCOPE,
        "resourceMetrics": {
            "aws": {"provider": "CLOUDWATCH_ECS", "allocationScope": "TASK"},
            "gcp": {"provider": "CLOUD_MONITORING_RUN", "allocationScope": "CONTAINER"},
            "access": "SERVER_CREDENTIALS_REQUIRED_NOT_CONNECTION_VERIFIED",
            "minimumTemporalCoveragePercent": 80,
            "requiredForSizing": ["cpuP95Percent", "memoryPeakMiB"],
        },
        "telemetryContract": {
            "applicationLabel": context.settings.ai_application_label,
            "environmentLabel": context.settings.ai_environment_label,
            "resourceAttributes": [
                "application.id",
                "deployment.environment.name",
                "foxtrot.owner.id",
                "foxtrot.deployment.id",
                "foxtrot.workload.kind",
            ],
            "metricNames": [
                "process_cpu_utilization_ratio",
                "process_memory_usage_bytes",
                "http_server_request_duration_seconds_bucket",
                "http_server_request_duration_seconds_count",
                "db_client_operation_duration_seconds_bucket",
            ],
        },
    }


@router.get("/deployments")
async def deployments(request: Request, session: CurrentSession) -> dict[str, Any]:
    return {
        "dataScope": DATA_SCOPE,
        "deployments": [
            scope.public()
            for scope in await service(request).infra.deployments(str(session.user.id))
        ],
    }


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    context: Context,
    session: CheckedSession,
    idempotency_key: IdempotencyKey = None,
) -> Any:
    return await once(
        context.repo,
        user_id=str(session.user.id),
        path=f"{DATA_SCOPE}:/v1/ai/chat",
        key=idempotency_key,
        body=body.model_dump(mode="json", by_alias=True),
        success_status=200,
        execute=lambda: service(request).run("chat", str(session.user.id), body),
    )


@router.post("/analyses")
async def analyse(
    body: AnalysisRequest,
    request: Request,
    context: Context,
    session: CheckedSession,
    idempotency_key: IdempotencyKey = None,
) -> Any:
    return await once(
        context.repo,
        user_id=str(session.user.id),
        path=f"{DATA_SCOPE}:/v1/ai/analyses",
        key=idempotency_key,
        body=body.model_dump(mode="json", by_alias=True),
        success_status=200,
        execute=lambda: service(request).run("analysis", str(session.user.id), body),
    )


@router.post("/recommendations")
async def recommend(
    body: RecommendationRequest,
    request: Request,
    context: Context,
    session: CheckedSession,
    idempotency_key: IdempotencyKey = None,
) -> Any:
    return await once(
        context.repo,
        user_id=str(session.user.id),
        path=f"{DATA_SCOPE}:/v1/ai/recommendations",
        key=idempotency_key,
        body=body.model_dump(mode="json", by_alias=True),
        success_status=200,
        execute=lambda: service(request).run("recommendation", str(session.user.id), body),
    )


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: str, request: Request, session: CurrentSession
) -> dict[str, Any]:
    store = service(request).store
    meta = await store.owned("CONVERSATION", conversation_id, str(session.user.id))
    return {
        "conversationId": conversation_id,
        "applicationId": meta["applicationId"],
        "environments": meta["result"]["environments"],
        "deploymentId": meta["result"].get("deploymentId"),
        "recentTurns": await store.history(conversation_id, str(session.user.id)),
    }


@router.get("/analyses/{analysis_id}")
async def analysis(analysis_id: str, request: Request, session: CurrentSession) -> dict[str, Any]:
    return dict(
        (await service(request).store.owned("ANALYSIS", analysis_id, str(session.user.id)))[
            "result"
        ]
    )


@router.get("/recommendations/{recommendation_id}")
async def recommendation(
    recommendation_id: str, request: Request, session: CurrentSession
) -> dict[str, Any]:
    return dict(
        (
            await service(request).store.owned(
                "RECOMMENDATION", recommendation_id, str(session.user.id)
            )
        )["result"]
    )
