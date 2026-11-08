from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import Any, TypedDict, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from control_plane.ai.allocation import allocation, target_baseline
from control_plane.ai.context import DATA_SCOPE, DeploymentScope, InfrastructureContext
from control_plane.ai.model import ModelProvider, Task
from control_plane.ai.schemas import (
    AnalysisDraft,
    AnalysisRequest,
    ChatDraft,
    ChatRequest,
    Parameter,
    RecommendationDraft,
    RecommendationRequest,
    TimeRange,
)
from control_plane.ai.store import AIStore, Kind
from control_plane.ai.telemetry import TelemetryReader
from control_plane.deployment.tools.render_inputs import validate_app, validate_sizing
from control_plane.errors import ApiError


class State(TypedDict, total=False):
    task: Task
    user: str
    request: ChatRequest | AnalysisRequest | RecommendationRequest
    payload: dict[str, Any]
    draft: Any
    result: dict[str, Any]


def invalid() -> ApiError:
    return ApiError(503, "AI_INVALID_RESPONSE", "model result failed evidence or sizing validation")


def parameters(values: list[Parameter], allowed: set[str]) -> tuple[dict[str, Any], dict[str, str]]:
    if len({value.key for value in values}) != len(values):
        raise invalid()
    if any(
        value.key not in allowed or not 1 <= len(value.rationale.strip()) <= 2000
        for value in values
    ):
        raise invalid()
    return {v.key: v.value for v in values}, {v.key: v.rationale for v in values}


class AIService:
    def __init__(
        self,
        infra: InfrastructureContext,
        store: AIStore,
        model: ModelProvider,
        telemetry: TelemetryReader,
    ) -> None:
        self.infra, self.store, self.model, self.telemetry = infra, store, model, telemetry
        graph = StateGraph(State)
        graph.add_node("collect", self._collect)
        graph.add_node("generate", self._generate)
        graph.add_node("validate", self._validate)
        graph.add_edge(START, "collect")
        graph.add_edge("collect", "generate")
        graph.add_edge("generate", "validate")
        graph.add_edge("validate", END)
        self.graph = graph.compile()

    async def run(
        self, task: Task, user: str, request: ChatRequest | AnalysisRequest | RecommendationRequest
    ) -> dict[str, Any]:
        state = await self.graph.ainvoke({"task": task, "user": user, "request": request})
        result = cast(dict[str, Any], state["result"])
        application = self.infra.application(request.application_id)
        if isinstance(request, ChatRequest):
            conversation = result["conversationId"]
            if request.conversation_id is None:
                await self.store.save(
                    "CONVERSATION",
                    conversation,
                    user,
                    application,
                    {
                        "applicationId": application,
                        "environments": state["payload"]["environments"],
                        "recentTurns": [],
                        "deploymentId": state["payload"].get("deploymentId"),
                    },
                )
            await self.store.append(conversation, user, request.message, result)
        else:
            kind: Kind = "ANALYSIS" if task == "analysis" else "RECOMMENDATION"
            identifier = result["analysisId" if task == "analysis" else "recommendationId"]
            await self.store.save(kind, identifier, user, application, result)
        return result

    async def _read_analysis(
        self, scope: DeploymentScope, request: AnalysisRequest
    ) -> dict[str, Any]:
        if request.environment and request.environment != scope.cloud:
            raise ApiError(
                422, "DEPLOYMENT_SCOPE_MISMATCH", "environment differs from the owned deployment"
            )
        if request.time_range.end.timestamp() > self.store.clock.now().timestamp() + 10:
            raise ApiError(422, "INVALID_TIME_RANGE", "analysis cannot include future time")
        payload = {
            "applicationId": scope.application,
            "sourceDeploymentId": scope.environment_id,
            "environment": scope.cloud,
            "source": self.infra.source(scope),
            "request": request.model_dump(mode="json", by_alias=True),
            **await self.telemetry.collect(scope, request),
        }
        payload["allowedEvidenceKeys"] = [
            f"metrics.{key}" for key, value in payload["metrics"].items() if value is not None
        ] + ["dataQuality.missingMetrics", "dataQuality.coveragePercent"]
        return payload

    def _analysis_result(
        self, request: AnalysisRequest, payload: dict[str, Any], draft: AnalysisDraft | None
    ) -> dict[str, Any]:
        return {
            "analysisId": f"analysis_{uuid4().hex}",
            "dataScope": DATA_SCOPE,
            "applicationId": payload["applicationId"],
            "sourceDeploymentId": request.deployment_id,
            "environment": payload["environment"],
            "timeRange": request.time_range.model_dump(mode="json"),
            "focus": request.focus,
            "status": payload["status"],
            "summary": draft.summary
            if draft
            else (
                "선택한 앱 배포의 운영 지표를 집계했습니다."
                if payload["status"] != "INSUFFICIENT_DATA"
                else "선택한 앱 배포에 분석할 운영 지표가 없습니다."
            ),
            "findings": [finding.model_dump(by_alias=True) for finding in draft.findings]
            if draft
            else [],
            **{
                key: payload[key]
                for key in ("metrics", "dataQuality", "resourceUsage", "traces", "tracesState")
            },
            "createdAt": self.store.clock.now().isoformat(),
        }

    async def _collect(self, state: State) -> dict[str, Any]:
        request, user = state["request"], state["user"]
        app = self.infra.application(request.application_id)
        payload: dict[str, Any]
        if isinstance(request, ChatRequest):
            selected = self.infra.environments(request.environments)
            deployment_id = request.deployment_id
            history = []
            if request.conversation_id:
                meta = await self.store.owned("CONVERSATION", request.conversation_id, user)
                previous = meta["result"]
                selected = self.infra.environments(request.environments or previous["environments"])
                deployment_id = deployment_id or previous.get("deploymentId")
                if (
                    meta["applicationId"] != app
                    or previous["environments"] != selected
                    or previous.get("deploymentId") != deployment_id
                ):
                    raise ApiError(
                        409,
                        "CONVERSATION_SCOPE_MISMATCH",
                        "start a new conversation for another scope",
                    )
                history = [
                    {"user": turn["user"], "assistant": turn["assistant"]["answer"]}
                    for turn in await self.store.history(request.conversation_id, user)
                ]
            payload = {
                "applicationId": app,
                "environments": selected,
                "deploymentId": deployment_id,
                "message": request.message,
                "history": history,
                "dataScope": DATA_SCOPE,
                "sources": await self.infra.collect(user, selected, deployment_id),
            }
        elif isinstance(request, AnalysisRequest):
            scope = await self.infra.owned(user, request.deployment_id, ready=True)
            payload = await self._read_analysis(scope, request)
        else:
            scope = await self.infra.owned(user, request.source_deployment_id, ready=True)
            catalog = self.infra.catalog(request.target_environment)
            # Allow for native cloud metrics publication latency.
            end = self.store.clock.now() - timedelta(minutes=3)
            query = AnalysisRequest(
                deployment_id=scope.environment_id,
                time_range=TimeRange(start=end - timedelta(hours=1), end=end),
            )
            readings = await self._read_analysis(scope, query)
            analysis = self._analysis_result(query, readings, None)
            await self.store.save("ANALYSIS", analysis["analysisId"], user, app, analysis)
            current = self.infra.source(scope)["data"]
            targets = [
                target
                for target in await self.infra.deployments(user)
                if target.cloud == request.target_environment
                and target.status in {"READY", "INFRA_READY", "APP_NOT_READY"}
            ]
            target = self.infra.source(targets[0])["data"] if len(targets) == 1 else None
            baseline = dict(target["sizing"] if target else catalog["baseline"])
            for key in ("db_storage_gib", "db_max_storage_gib"):
                baseline[key] = max(baseline[key], current["sizing"].get(key, 0))
            catalog["baseline"] = baseline
            core = ["cpuP95Percent", "memoryPeakMiB"]
            missing = [
                key
                for key in core
                if analysis["metrics"][key] is None
                or (
                    analysis["dataQuality"]["metrics"][key]["temporalCoveragePercent"] is not None
                    and analysis["dataQuality"]["metrics"][key]["temporalCoveragePercent"] < 80
                )
            ]
            if not analysis["resourceUsage"]:
                missing = list(dict.fromkeys([*missing, "resourceUsage"]))
            app_baseline = target_baseline(
                request.target_environment, current, target, catalog["appAllocations"]["default"]
            )
            # New target tunnel configuration is not observed yet. Reserve its possible capacity.
            target_topology = target or {"hasTunnelContainer": True}
            bounds = {}
            candidates = []
            if not missing:
                usage = analysis["resourceUsage"]
                cpu_usage = usage["cpuP95Cores"]
                memory_usage = usage["memoryPeakMiB"]
                if request.target_environment == "gcp":
                    cpu_usage = usage.get("containerCpuUpperBoundCores", cpu_usage)
                    memory_usage = usage.get("containerMemoryPeakMiB", memory_usage)
                # A total is a conservative per-container bound when container CPU is unknown.
                bounds = {
                    "minimumCpuCores": cpu_usage / 0.7,
                    "minimumMemoryMiB": math.ceil(memory_usage / 0.8),
                    "cpuTargetPercent": 70,
                    "memoryTargetPercent": 80,
                    "method": "CONSERVATIVE_WHOLE_WORKLOAD_BOUND",
                }
                for cpu in catalog["appAllocations"]["cpu"]:
                    for memory in range(512, 32769, 512):
                        if cpu < bounds["minimumCpuCores"] or memory < bounds["minimumMemoryMiB"]:
                            continue
                        candidate = {**app_baseline, "cpu": cpu, "memory_mib": memory}
                        try:
                            self._validate_app(
                                request.target_environment, candidate, target_topology
                            )
                        except ValueError:
                            continue
                        candidates.append({"cpu": cpu, "memory_mib": memory})
            existing = current["sizing"]
            target_sizing = target["sizing"] if target else {}
            availability = {
                "database": bool(
                    existing.get("db_multi_az")
                    or existing.get("db_high_availability")
                    or target_sizing.get("db_multi_az")
                    or target_sizing.get("db_high_availability")
                ),
                "cache": bool(
                    existing.get("cache_high_availability")
                    or existing.get("cache_node_count", 1) > 1
                    or target_sizing.get("cache_high_availability")
                    or target_sizing.get("cache_node_count", 1) > 1
                ),
            }
            allowed = []
            for key in catalog["parameters"]:
                if (
                    (key in {"db_multi_az", "db_high_availability"} and availability["database"])
                    or (
                        key in {"cache_node_count", "cache_high_availability"}
                        and availability["cache"]
                    )
                    or (
                        key in {"db_instance_class", "db_tier"}
                        and analysis["metrics"]["databaseP95Ms"] is not None
                    )
                    or (key.startswith("cache_") and analysis["metrics"]["redisP95Ms"] is not None)
                ):
                    allowed.append(key)
            payload = {
                "applicationId": app,
                "dataScope": DATA_SCOPE,
                "cloud": request.target_environment,
                "catalog": catalog,
                "sources": [self.infra.source(scope)],
                "current": current,
                "target": target,
                "analysis": analysis,
                "allowedParameters": allowed,
                "preserveAvailability": availability,
                "missingRequiredMetrics": missing,
                "appBaseline": app_baseline,
                "resourceBounds": bounds,
                "allowedAppAllocations": candidates,
                "targetTopology": target_topology,
            }
        return {"payload": payload}

    async def _generate(self, state: State) -> dict[str, Any]:
        payload = state["payload"]
        if (state["task"] == "analysis" and payload["status"] == "INSUFFICIENT_DATA") or (
            state["task"] == "recommendation"
            and (payload["missingRequiredMetrics"] or not payload["allowedAppAllocations"])
        ):
            return {"draft": None}
        return {"draft": await self.model.generate(state["task"], payload)}

    def _validate(self, state: State) -> dict[str, Any]:
        request, payload, draft = state["request"], state["payload"], state["draft"]
        now = self.store.clock.now().isoformat()
        result: dict[str, Any]
        if isinstance(request, ChatRequest):
            if not isinstance(draft, ChatDraft) or not 1 <= len(draft.answer.strip()) <= 16000:
                raise invalid()
            sources = {source["id"]: source for source in payload["sources"]}
            if not draft.source_ids or set(draft.source_ids) - sources.keys():
                raise invalid()
            result = {
                "conversationId": request.conversation_id or f"conv_{uuid4().hex}",
                "messageId": f"msg_{uuid4().hex}",
                "answer": draft.answer,
                "sources": [sources[key] for key in dict.fromkeys(draft.source_ids)],
                "warnings": draft.warnings,
                "createdAt": now,
            }
        elif isinstance(request, AnalysisRequest):
            if draft is not None:
                if not isinstance(draft, AnalysisDraft):
                    raise invalid()
                for finding in draft.findings:
                    # Canonicalize known bare metric keys; unknown or null evidence stays invalid.
                    finding.evidence_keys = [
                        f"metrics.{key}" if key in payload["metrics"] else key
                        for key in finding.evidence_keys
                    ]
                    allowed = set(payload["allowedEvidenceKeys"])
                    if (
                        not finding.evidence_keys
                        or set(finding.evidence_keys) - allowed
                        or (
                            finding.category != "DATA_QUALITY"
                            and not any(key.startswith("metrics.") for key in finding.evidence_keys)
                        )
                    ):
                        raise invalid()
            result = self._analysis_result(request, payload, draft)
        else:
            if payload["missingRequiredMetrics"] or not payload["allowedAppAllocations"]:
                missing_data = bool(payload["missingRequiredMetrics"])
                return {
                    "result": {
                        "recommendationId": f"rec_{uuid4().hex}",
                        "applicationId": payload["applicationId"],
                        "dataScope": DATA_SCOPE,
                        "sourceDeploymentId": request.source_deployment_id,
                        "targetEnvironment": request.target_environment,
                        "status": "INSUFFICIENT_DATA" if missing_data else "CONSTRAINT_LIMIT",
                        "basis": "TELEMETRY",
                        "analysisId": payload["analysis"]["analysisId"],
                        "analysis": payload["analysis"],
                        "missingRequiredMetrics": payload["missingRequiredMetrics"],
                        "resourceBounds": payload["resourceBounds"],
                        "proposal": None,
                        "appProposal": None,
                        "warnings": [
                            "선택한 앱의 CPU·메모리 지표 또는 관측 시간이 부족합니다."
                            if missing_data
                            else "관측 사용량을 수용하는 조합이 "
                            "대상 환경의 현재 사양 정책에 없습니다."
                        ],
                        "createdAt": now,
                    }
                }
            if not isinstance(draft, RecommendationDraft):
                raise invalid()
            cloud, catalog, current = payload["cloud"], payload["catalog"], payload["current"]
            params, rationale = parameters(draft.parameters, set(payload["allowedParameters"]))
            app_params, app_rationale = parameters(draft.app_parameters, {"cpu", "memory_mib"})
            baseline = catalog["baseline"]
            target = payload["target"]
            proposal = {"cloud": cloud, "parameters": params, "rationale": rationale}
            try:
                validate_sizing(cloud, params, complete=False)  # type: ignore[no-untyped-call]
                effective = {**baseline, **params}
                validate_sizing(cloud, effective)  # type: ignore[no-untyped-call]
                app_baseline = {**payload["appBaseline"], **payload["allowedAppAllocations"][0]}
                effective_app = {**app_baseline, **app_params}
                self._validate_app(cloud, effective_app, payload["targetTopology"])
                if {key: effective_app[key] for key in ("cpu", "memory_mib")} not in payload[
                    "allowedAppAllocations"
                ]:
                    raise ValueError("allocation below measured resource bounds")
                availability = payload["preserveAvailability"]
                if (
                    availability["database"]
                    and not effective["db_multi_az" if cloud == "aws" else "db_high_availability"]
                ):
                    raise ValueError("existing database availability must be preserved")
                if availability["cache"] and not (
                    effective["cache_node_count"] >= 2
                    if cloud == "aws"
                    else effective["cache_high_availability"]
                ):
                    raise ValueError("existing cache availability must be preserved")
            except (ValueError, KeyError, TypeError) as exc:
                raise invalid() from exc
            warnings = [
                *draft.warnings,
                "추천은 자동 배포되지 않습니다. 배포 API로 별도 적용해야 합니다.",
                "실제 부하 시험으로 사양과 지연 목표를 확인해야 합니다.",
                "compute_* 항목은 VM 사양입니다. "
                "Fargate·Cloud Run 앱 할당량은 appProposal을 확인하세요.",
                "최근 1시간의 분 단위 관측으로 계산했습니다. "
                "일간·주간 최대 부하는 확인하지 않았습니다.",
            ]
            if current["environment"] != cloud:
                warnings.append(
                    "클라우드 전환 시 전체 사용량을 보수적으로 적용합니다. "
                    "컨테이너별 CPU 병목과 성능 차이는 전환 전 부하 시험으로 확인해야 합니다."
                )
            analysis = payload["analysis"]
            if analysis["status"] != "COMPLETED":
                warnings.append("일부 지표가 누락되어 해당 자원의 변경 추천을 제한했습니다.")
            target_id = target["environmentId"] if target else None
            update_path = f"/v1/provisioning/environments/{target_id}/update"
            result = {
                "recommendationId": f"rec_{uuid4().hex}",
                "applicationId": payload["applicationId"],
                "status": "COMPLETED",
                "analysisStatus": analysis["status"],
                "dataScope": DATA_SCOPE,
                "sourceDeploymentId": request.source_deployment_id,
                "targetEnvironment": request.target_environment,
                "targetDeploymentId": target_id,
                "basis": "TELEMETRY",
                "analysis": analysis,
                "analysisId": analysis["analysisId"],
                "currentParameters": current.get("sizing") if current else None,
                "validationBaseline": baseline,
                "baselineSource": "LAST_SUCCESSFUL_APPLY" if target else catalog["baselineSource"],
                "proposal": proposal,
                "effectiveParameters": effective,
                "appProposal": {
                    "parameters": {key: effective_app[key] for key in ("cpu", "memory_mib")},
                    "rationale": {
                        "cpu": "분 단위 CPU P95와 목표 사용률 70%를 기준으로 계산했습니다.",
                        "memory_mib": "관측 메모리와 목표 사용률 80%를 기준으로 계산했습니다.",
                        **app_rationale,
                    },
                },
                "effectiveAppAllocation": effective_app,
                "allocation": {
                    **allocation(
                        cloud,
                        effective_app,
                        bool(payload["targetTopology"].get("hasTunnelContainer")),
                    ),
                    "tunnelBasis": "OBSERVED_TARGET" if target else "RESERVED_IF_CONFIGURED",
                },
                "resourceBounds": payload["resourceBounds"],
                "iac": {
                    "action": "UPDATE" if target else "CREATE",
                    "endpoint": update_path
                    if target
                    else "/v1/applications/" + payload["applicationId"] + "/deployments",
                    "body": {
                        **({"cloud": cloud} if not target else {}),
                        "sizing": {
                            key: value
                            for key, value in effective.items()
                            if not target or target["sizing"].get(key) != value
                        },
                        "app": {
                            "cpu": effective_app["cpu"],
                            "memoryMiB": effective_app["memory_mib"],
                            **(
                                {
                                    "workerCount": effective_app["worker_count"],
                                    "apiWorkers": effective_app.get("api_workers", 4),
                                }
                                if not target
                                else {}
                            ),
                        },
                    },
                },
                "warnings": warnings,
                "createdAt": now,
            }
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 250000:
            raise invalid()
        return {"result": result}

    @staticmethod
    def _validate_app(cloud: str, app: dict[str, Any], topology: dict[str, Any]) -> None:
        validate_app(  # type: ignore[no-untyped-call]
            cloud,
            {
                **app,
                "image": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/validation"
                if cloud == "aws"
                else "asia-northeast3-docker.pkg.dev/validation/apps/validation",
                "entrypoint": ["platform-managed"],
                "tunnel_secret": "configured" if topology.get("hasTunnelContainer") else "",
            },
        )
