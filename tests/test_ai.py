from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from control_plane.ai.allocation import allocation
from control_plane.ai.context import DATA_SCOPE
from control_plane.ai.model import OpenAIModel, Task
from control_plane.ai.native_metrics import summarize
from control_plane.ai.schemas import AnalysisDraft, AnalysisRequest, ChatDraft, RecommendationDraft
from control_plane.ai.telemetry import TelemetryReader
from control_plane.api.idempotency import once
from control_plane.config import Settings
from control_plane.deployment.api import org_id
from control_plane.deployment.provisioner.service import Service
from control_plane.deployment.provisioner.spec import RequestError
from control_plane.deployment.provisioner.store import Store
from control_plane.deployment.provisioner.terraform import TerraformError
from control_plane.errors import ApiError
from tests.deployment.test_provisioner import CATALOG, CONFIG, FakeTerraform, ReadyChecker
from tests.test_api import Console
from tests.test_api import console as console

USER = "github:4242"


class FakeModel:
    configured = True

    def __init__(self) -> None:
        self.calls: list[tuple[Task, dict[str, Any]]] = []
        self.reply: BaseModel | None = None

    async def generate(self, task: Task, payload: dict[str, Any]) -> BaseModel:
        self.calls.append((task, payload))
        if self.reply:
            return self.reply
        if task == "chat":
            return ChatDraft(
                answer="내 앱 배포 기록입니다.",
                source_ids=[payload["sources"][0]["id"]],
                warnings=[],
            )
        if task == "analysis":
            return AnalysisDraft(
                summary="CPU 사용률을 확인하세요.",
                findings=[
                    {
                        "category": "RESOURCE",
                        "severity": "WARNING",
                        "description": "CPU 사용률입니다.",
                        "evidenceKeys": ["metrics.cpuP95Percent"],
                    }
                ],
            )
        return RecommendationDraft(
            parameters=[],
            app_parameters=[
                {"key": "cpu", "value": 2, "rationale": "측정된 CPU 사용률에 맞춰 늘립니다."},
                {
                    "key": "memory_mib",
                    "value": 4096,
                    "rationale": "측정된 메모리와 대상 CPU 할당 기준입니다.",
                },
            ],
            warnings=[],
        )


def drain(engine: Service) -> None:
    while engine.run_next():
        pass


@pytest.fixture
def model(console: Console, tmp_path: Path) -> FakeModel:
    engine = Service(
        Store(tmp_path / "p.db"),
        FakeTerraform(tmp_path),
        copy.deepcopy(CATALOG),
        copy.deepcopy(CONFIG),
        checker=ReadyChecker(),
    )
    for cloud in ("aws", "gcp"):
        engine.create({"org": {"id": org_id(USER, "goods"), "name": "goods"}, "cloud": cloud})
    drain(engine)
    console.app.state.ai_service.infra.deployment = engine
    fake = FakeModel()
    console.app.state.ai_service.model = fake
    return fake


def source_id(console: Console, cloud: str = "aws") -> str:
    return str(
        next(
            record["id"]
            for record in console.app.state.ai_service.infra.deployment.store.environments()
            if record["cloud"] == cloud and record["org_id"] == org_id(USER, "goods")
        )
    )


def analysis_body(console: Console) -> dict[str, Any]:
    end = console.world.clock.now()
    return {
        "deploymentId": source_id(console),
        "timeRange": {"start": (end - timedelta(minutes=5)).isoformat(), "end": end.isoformat()},
    }


async def post(console: Console, path: str, body: dict[str, Any]) -> httpx.Response:
    return await console.http.post(f"/v1/ai/{path}", json=body, headers=console.change_headers())


def prometheus(request: httpx.Request) -> httpx.Response:
    query = request.url.params["query"]
    assert 'foxtrot_workload_kind="USER_APPLICATION"' in query
    assert f'foxtrot_owner_id="{org_id(USER, "goods")}"' in query
    assert 'foxtrot_deployment_id="' in query
    end = float(request.url.params.get("end", request.url.params.get("time", "0")))
    if request.url.path.endswith("query_range"):
        start, step = float(request.url.params["start"]), float(request.url.params["step"])
        values = [
            [start + i * step, "90" if "cpu" in query else "800"]
            for i in range(int((end - start) / step) + 1)
        ]
        result = [{"metric": {}, "values": values}]
    elif "http" in query:
        result = [{"metric": {}, "value": [end, "5"]}]
    else:
        result = []
    return httpx.Response(200, json={"status": "success", "data": {"result": result}})


class FakeNativeMetrics:
    async def collect(self, scope: Any, request: AnalysisRequest) -> dict[str, Any]:
        allocated = allocation(scope.cloud, scope.snapshot["app"])
        return {
            "metrics": {"cpuP95Percent": 90, "memoryPeakMiB": 800},
            "quality": {
                key: {
                    "state": "OK",
                    "samples": 60,
                    "source": "CLOUDWATCH_ECS",
                    "temporalCoveragePercent": 100,
                    "aggregation": "MINUTE_AGGREGATE",
                }
                for key in ("cpuP95Percent", "memoryPeakMiB")
            },
            "resourceUsage": {
                "source": "CLOUDWATCH_ECS",
                "allocation": allocated,
                "cpuP95Cores": 0.9,
                "memoryPeakMiB": 800,
            },
        }


def telemetry(console: Console, http: httpx.AsyncClient) -> None:
    console.app.state.ai_service.telemetry = TelemetryReader(
        Settings(prometheus_url="http://prometheus", _env_file=None),
        http,
        native=FakeNativeMetrics(),
    )


async def test_chat_history_scope_owner_and_idempotency(console: Console, model: FakeModel) -> None:
    await console.login()
    headers = console.change_headers()
    body = {
        "message": "내 배포를 알려줘",
        "environments": ["aws"],
        "deploymentId": source_id(console),
    }
    response = await console.http.post("/v1/ai/chat", json=body, headers=headers)
    assert response.status_code == 200, response.text
    first = response.json()
    assert (await console.http.post("/v1/ai/chat", json=body, headers=headers)).json() == first
    assert len(model.calls) == 1
    body.update(conversationId=first["conversationId"], message="CPU도 알려줘")
    assert (await post(console, "chat", body)).status_code == 200
    assert len(model.calls[-1][1]["history"]) == 1
    history = (await console.http.get(f"/v1/ai/conversations/{first['conversationId']}")).json()
    assert len(history["recentTurns"]) == 2 and history["deploymentId"] == source_id(console)
    body["deploymentId"] = source_id(console, "gcp")
    assert (await post(console, "chat", body)).status_code == 409
    with pytest.raises(ApiError) as exc:
        await console.app.state.ai_service.store.owned(
            "CONVERSATION", first["conversationId"], "stranger"
        )
    assert exc.value.status == 404


async def test_platform_seed_workflows_and_foreign_apps_never_reach_model(
    console: Console, model: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = console.app.state.ai_service.infra.deployment
    engine.create(
        {
            "org": {"id": org_id("stranger", "goods"), "name": "PLATFORM_PRIVATE"},
            "cloud": "aws",
            "app": {"env": {"SECRET_VALUE": "never-send"}},
        }
    )
    drain(engine)

    def forbidden(*args: Any) -> Any:
        raise AssertionError("AI must not query platform workflows or bindings")

    monkeypatch.setattr(console.world.repo, "active_workflow_id", forbidden)
    monkeypatch.setattr(console.world.repo, "application_binding", forbidden)
    await console.login()
    assert (await post(console, "chat", {"message": "인프라 알려줘"})).status_code == 200
    payload = model.calls[-1][1]
    assert len(payload["sources"]) == 3
    assert all(s["type"].startswith("USER_APPLICATION") for s in payload["sources"])
    serialized = json.dumps(payload)
    for secret in (
        "PLATFORM_PRIVATE",
        "never-send",
        "actorProcesses",
        "configured-application",
        "sourceRef",
        "workflowId",
        "secretRef",
        "dockerHost",
        "tokenSecret",
    ):
        assert secret not in serialized
    inventory = (await console.http.get("/v1/ai/deployments")).json()
    assert len(inventory["deployments"]) == 2


async def test_no_deployments_does_not_fall_back_to_shared_configuration(
    console: Console, model: FakeModel
) -> None:
    console.app.state.ai_service.infra.deployment = None
    await console.login()
    assert (await console.http.get("/v1/ai/deployments")).json()["deployments"] == []
    assert (await post(console, "chat", {"message": "내 앱 알려줘"})).status_code == 200
    assert model.calls[-1][1]["sources"][0]["data"]["deploymentCount"] == 0
    assert len(model.calls[-1][1]["sources"]) == 1


async def test_auth_csrf_idempotency_required(console: Console, model: FakeModel) -> None:
    assert (await console.http.get("/v1/ai/deployments")).status_code == 401
    assert (await console.http.post("/v1/ai/chat", json={"message": "hi"})).status_code == 401
    await console.login()
    assert (await console.http.post("/v1/ai/chat", json={"message": "hi"})).status_code == 403
    headers = console.change_headers()
    del headers["Idempotency-Key"]
    assert (
        await console.http.post("/v1/ai/chat", json={"message": "hi"}, headers=headers)
    ).status_code == 400
    assert not model.calls


async def test_old_platform_conversations_and_replayed_answers_are_inaccessible(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    from control_plane.ai.schemas import ChatRequest

    body = ChatRequest(message="hi").model_dump(mode="json", by_alias=True)

    async def legacy() -> dict[str, Any]:
        return {"answer": "PLATFORM_PRIVATE"}

    await once(
        console.world.repo,
        user_id=USER,
        path="/v1/ai/chat",
        key="old-key",
        body=body,
        success_status=200,
        execute=legacy,
    )
    result = await console.http.post(
        "/v1/ai/chat",
        json={"message": "hi"},
        headers=console.change_headers(**{"Idempotency-Key": "old-key"}),
    )
    assert result.status_code == 200 and "PLATFORM_PRIVATE" not in result.text
    identifier = result.json()["conversationId"]
    item = await console.world.table.get(f"AI_CONVERSATION#{identifier}", "META")
    value = json.loads(item.body)
    value.pop("dataScope")
    await console.world.table.put(
        item.pk, item.sk, json.dumps(value), expected_version=item.version
    )
    assert (await console.http.get(f"/v1/ai/conversations/{identifier}")).status_code == 404
    assert (
        await post(console, "chat", {"conversationId": identifier, "message": "continue"})
    ).status_code == 404


async def test_recommendation_collects_native_metrics_without_manual_requirements(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    async with httpx.AsyncClient(transport=httpx.MockTransport(prometheus)) as http:
        telemetry(console, http)
        for cloud in ("aws", "gcp"):
            response = await post(
                console,
                "recommendations",
                {"sourceDeploymentId": source_id(console), "targetEnvironment": cloud},
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["basis"] == "TELEMETRY" and result["dataScope"] == DATA_SCOPE
            assert result["analysis"]["metrics"]["cpuP95Percent"] == 90
            assert result["effectiveAppAllocation"]["cpu"] == 2
            assert result["baselineSource"] == "LAST_SUCCESSFUL_APPLY"
            assert result["iac"]["action"] == "UPDATE"
            assert source_id(console, cloud) in result["iac"]["endpoint"]
            assert (
                await console.http.get(f"/v1/ai/analyses/{result['analysisId']}")
            ).status_code == 200
            assert (
                await console.http.get(f"/v1/ai/recommendations/{result['recommendationId']}")
            ).json() == result
    assert [task for task, _ in model.calls] == ["recommendation", "recommendation"]
    assert all("requirements" not in payload for _, payload in model.calls)


async def test_missing_native_metrics_blocks_recommendation_without_model(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    result = (
        await post(
            console,
            "recommendations",
            {"sourceDeploymentId": source_id(console), "targetEnvironment": "gcp"},
        )
    ).json()
    assert result["status"] == "INSUFFICIENT_DATA" and result["proposal"] is None
    assert result["missingRequiredMetrics"] and not model.calls
    analysis = (await post(console, "analyses", analysis_body(console))).json()
    assert analysis["status"] == "INSUFFICIENT_DATA"
    assert all(v is None for v in analysis["metrics"].values())
    console.world.clock.advance(31 * 86400)
    await console.login()
    assert (await console.http.get(f"/v1/ai/analyses/{analysis['analysisId']}")).status_code == 404


@pytest.mark.parametrize("coverage", [79, 80, 100])
async def test_cpu_memory_are_sufficient_without_http_or_otel(
    console: Console,
    model: FakeModel,
    coverage: int,
) -> None:
    class Native(FakeNativeMetrics):
        async def collect(self, scope: Any, request: AnalysisRequest) -> dict[str, Any]:
            result = await super().collect(scope, request)
            for metric in result["quality"].values():
                metric["temporalCoveragePercent"] = coverage
            return result

    await console.login()
    async with httpx.AsyncClient() as http:
        console.app.state.ai_service.telemetry = TelemetryReader(
            Settings(_env_file=None),
            http,
            native=Native(),
        )
        response = await post(
            console,
            "recommendations",
            {
                "sourceDeploymentId": source_id(console),
                "targetEnvironment": "gcp",
            },
        )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["analysis"]["metrics"]["requestP95Ms"] is None
    if coverage >= 80:
        assert result["status"] == "COMPLETED"
        assert result["analysisStatus"] == "PARTIAL"
        assert result["allocation"]["scope"] == "CONTAINER"
        assert result["allocation"]["totalCpuCores"] == 6
        assert [task for task, _ in model.calls] == ["recommendation"]
        assert result["resourceBounds"]["minimumCpuCores"] > 1
    else:
        assert result["status"] == "INSUFFICIENT_DATA" and not model.calls


async def test_unsupported_capacity_is_not_a_fabricated_recommendation(
    console: Console,
    model: FakeModel,
) -> None:
    class Native(FakeNativeMetrics):
        async def collect(self, scope: Any, request: AnalysisRequest) -> dict[str, Any]:
            result = await super().collect(scope, request)
            result["resourceUsage"]["cpuP95Cores"] = 6
            return result

    await console.login()
    async with httpx.AsyncClient() as http:
        console.app.state.ai_service.telemetry = TelemetryReader(
            Settings(_env_file=None),
            http,
            native=Native(),
        )
        response = await post(
            console,
            "recommendations",
            {
                "sourceDeploymentId": source_id(console),
                "targetEnvironment": "gcp",
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "CONSTRAINT_LIMIT" and not model.calls


async def test_foreign_unapplied_destroyed_or_wrong_environment_blocks_backend_reads(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    engine = console.app.state.ai_service.infra.deployment
    _, foreign = engine.create({"org": {"id": org_id("stranger", "goods")}, "cloud": "aws"})
    drain(engine)
    seen = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return prometheus(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        telemetry(console, http)
        for identifier in (foreign["environmentId"], "env-" + "0" * 12):
            assert (
                await post(
                    console,
                    "recommendations",
                    {"sourceDeploymentId": identifier, "targetEnvironment": "gcp"},
                )
            ).status_code == 404
            assert (
                await post(
                    console, "analyses", {**analysis_body(console), "deploymentId": identifier}
                )
            ).status_code == 404
            assert (
                await post(console, "chat", {"deploymentId": identifier, "message": "hi"})
            ).status_code == 404
        assert (
            await post(console, "analyses", {**analysis_body(console), "environment": "gcp"})
        ).status_code == 422
        record = engine.store.environment(source_id(console))
        engine.store.update_environment(record["id"], record["updated_at"], status="DESTROYED")
        assert (await post(console, "analyses", analysis_body(console))).status_code == 404
    assert not seen and not model.calls


@pytest.mark.parametrize(
    "draft",
    [
        {"parameters": [{"key": "db_storage_gib", "value": 30, "rationale": "no storage metrics"}]},
        {
            "parameters": [
                {"key": "db_instance_class", "value": "db.m6g.large", "rationale": "no DB metrics"}
            ]
        },
        {
            "app_parameters": [
                {"key": "memory_mib", "value": 512, "rationale": "invalid combination"}
            ]
        },
        {"app_parameters": [{"key": "image", "value": "malicious", "rationale": "forbidden"}]},
        {"app_parameters": [{"key": "cpu", "value": True, "rationale": "wrong type"}]},
        {
            "app_parameters": [
                {"key": "cpu", "value": 1, "rationale": "below measured headroom"},
                {"key": "memory_mib", "value": 2048, "rationale": "valid Fargate pair"},
            ]
        },
        {
            "app_parameters": [
                {"key": "cpu", "value": 2, "rationale": "a"},
                {"key": "cpu", "value": 4, "rationale": "b"},
            ]
        },
    ],
)
async def test_invalid_or_unmeasured_proposals_rejected(
    console: Console, model: FakeModel, draft: dict[str, Any]
) -> None:
    await console.login()
    model.reply = RecommendationDraft.model_validate(
        {"parameters": [], "app_parameters": [], "warnings": [], **draft}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(prometheus)) as http:
        telemetry(console, http)
        response = await post(
            console,
            "recommendations",
            {"sourceDeploymentId": source_id(console), "targetEnvironment": "aws"},
        )
    assert response.status_code == 503, response.text
    assert not (await console.world.table.query(f"USER#{USER}", "AI#RECOMMENDATION#")).items


async def test_analysis_evidence_normalized_but_missing_metrics_rejected(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    model.reply = AnalysisDraft(
        summary="CPU와 데이터 부족",
        findings=[
            {
                "category": "RESOURCE",
                "severity": "WARNING",
                "description": "CPU 사용률",
                "evidenceKeys": ["cpuP95Percent", "dataQuality.coveragePercent"],
            }
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(prometheus)) as http:
        telemetry(console, http)
        response = await post(console, "analyses", analysis_body(console))
        assert response.status_code == 200
        assert response.json()["findings"][0]["evidenceKeys"][0] == "metrics.cpuP95Percent"
        model.reply = AnalysisDraft(
            summary="invalid",
            findings=[
                {
                    "category": "PERFORMANCE",
                    "severity": "CRITICAL",
                    "description": "invented DB issue",
                    "evidenceKeys": ["metrics.databaseP95Ms"],
                }
            ],
        )
        assert (await post(console, "analyses", analysis_body(console))).status_code == 503


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-1", "101"])
async def test_nonfinite_invalid_and_stale_telemetry_is_missing(
    console: Console, model: FakeModel, value: str
) -> None:
    query = AnalysisRequest.model_validate({**analysis_body(console), "focus": ["RESOURCES"]})
    scope = await console.app.state.ai_service.infra.owned(USER, query.deployment_id)
    scope.snapshot["appliedAt"] = query.time_range.start.isoformat()
    end = int(query.time_range.end.timestamp())
    result = summarize(
        scope,
        query,
        {"cpuP95Percent": {end - 60: float(value), end + 60: 5}, "memoryPeakMiB": {}},
        "CLOUDWATCH_ECS",
        allocation(scope.cloud, scope.snapshot["app"]),
        memory_scale=1,
        cpu_aggregation="TEST",
        memory_aggregation="TEST",
    )
    assert result["metrics"]["cpuP95Percent"] is None


async def test_tempo_queries_have_owner_deployment_and_user_app_filters(
    console: Console, model: FakeModel
) -> None:
    scope = await console.app.state.ai_service.infra.owned(USER, source_id(console))
    seen = []

    def backend(request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        for term in (scope.owner_id, scope.deployment_id, "USER_APPLICATION"):
            assert term in query
        seen.append(query)
        return httpx.Response(200, json={"traces": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        await TelemetryReader(Settings(tempo_url="http://tempo", _env_file=None), http).collect(
            scope, AnalysisRequest.model_validate(analysis_body(console))
        )
    assert len(seen) == 1


async def test_applied_snapshot_survives_failed_update_and_omits_secrets(
    console: Console, model: FakeModel
) -> None:
    engine = console.app.state.ai_service.infra.deployment
    env_id = source_id(console)
    previous = engine.get(env_id)["appliedSnapshot"]
    engine.update(env_id, {"sizing": {"db_storage_gib": 30}})

    def fail(*args: Any) -> None:
        raise TerraformError("failed apply")

    engine.tf.apply = fail
    drain(engine)
    assert engine.get(env_id)["appliedSnapshot"] == previous
    with pytest.raises(ApiError) as exc:
        await console.app.state.ai_service.infra.owned(USER, env_id, ready=True)
    assert exc.value.status == 409


@pytest.mark.parametrize("field", ["env", "apiEnv", "workerEnv", "secretEnv"])
async def test_resource_scope_labels_are_server_owned(
    console: Console, model: FakeModel, field: str
) -> None:
    engine = console.app.state.ai_service.infra.deployment
    record = engine.store.environment(source_id(console))
    attributes = record["tfvars"]["app"]["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    for term in (
        org_id(USER, "goods"),
        record["deployment_id"],
        "foxtrot.workload.kind=USER_APPLICATION",
    ):
        assert term in attributes
    with pytest.raises(RequestError):
        engine.update(
            record["id"],
            {"app": {field: {"OTEL_RESOURCE_ATTRIBUTES": "foxtrot.owner.id=someone-else"}}},
        )


async def test_legacy_manual_requirements_are_no_longer_accepted(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    response = await post(
        console,
        "recommendations",
        {
            "sourceDeploymentId": source_id(console),
            "targetEnvironment": "gcp",
            "requirements": {"expectedPeakRps": 100},
        },
    )
    assert response.status_code == 422 and not model.calls


async def test_missing_key_and_provider_failure_are_safe() -> None:
    with pytest.raises(ApiError) as exc:
        await OpenAIModel(None, "gpt-5-mini", 1, 1000).generate("chat", {})
    assert exc.value.code == "AI_NOT_CONFIGURED"

    class Broken:
        def with_structured_output(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("SECRET_VALUE")

    provider = OpenAIModel(None, "gpt-5-mini", 1, 1000)
    provider.configured, provider._client = True, Broken()
    with pytest.raises(ApiError) as exc:
        await provider.generate("chat", {})
    assert exc.value.code == "AI_UNAVAILABLE" and "SECRET_VALUE" not in exc.value.message


async def test_model_schema_restricts_evidence_to_existing_values() -> None:
    class StructuredClient:
        def with_structured_output(self, schema: dict[str, Any], **kwargs: Any) -> StructuredClient:
            assert schema["$defs"]["Finding"]["properties"]["evidenceKeys"]["items"]["enum"] == [
                "metrics.cpuP95Percent"
            ]
            return self

        async def ainvoke(self, messages: Any) -> dict[str, Any]:
            return {"summary": "CPU 분석", "findings": []}

    provider = OpenAIModel(None, "gpt-5-mini", 1, 1000)
    provider.configured, provider._client = True, StructuredClient()
    assert isinstance(
        await provider.generate("analysis", {"allowedEvidenceKeys": ["metrics.cpuP95Percent"]}),
        AnalysisDraft,
    )


async def test_local_playground_has_user_app_ui_and_no_deployment_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.ai import local

    monkeypatch.setattr(local, "Settings", lambda **kwargs: Settings(**kwargs, _env_file=None))
    app = local.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Foxtrot AI" in page.text
        assert "source-deployment" in page.text and "peak-rps" not in page.text
        assert (await client.get("/playground/app.js")).status_code == 200
        assert (await client.get("/v1/ai/deployments")).json()["deployments"] == []
        assert (await client.get("/playground/context")).json()["environments"] == []
        assert (await client.get("/v1/workflows")).status_code == 404
        assert (await client.get("/v1/applications/goods/deployments")).status_code == 404


async def test_new_target_uses_catalog_and_preserves_source_storage(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    engine = console.app.state.ai_service.infra.deployment
    engine.destroy(source_id(console, "gcp"))
    drain(engine)
    engine.update(source_id(console), {"sizing": {"db_storage_gib": 50}})
    drain(engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(prometheus)) as http:
        telemetry(console, http)
        result = (
            await post(
                console,
                "recommendations",
                {"sourceDeploymentId": source_id(console), "targetEnvironment": "gcp"},
            )
        ).json()
    assert result["iac"]["action"] == "CREATE"
    assert result["iac"]["body"]["sizing"]["db_storage_gib"] == 50
    assert result["targetDeploymentId"] is None


async def test_existing_availability_cannot_be_downgraded(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    engine = console.app.state.ai_service.infra.deployment
    engine.update(source_id(console), {"sizing": {"db_multi_az": True, "cache_node_count": 2}})
    drain(engine)
    body = {"sourceDeploymentId": source_id(console), "targetEnvironment": "gcp"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(prometheus)) as http:
        telemetry(console, http)
        assert (await post(console, "recommendations", body)).status_code == 503
        model.reply = RecommendationDraft(
            parameters=[
                {"key": "db_high_availability", "value": True, "rationale": "기존 DB 가용성 유지"},
                {
                    "key": "cache_high_availability",
                    "value": True,
                    "rationale": "기존 Redis 가용성 유지",
                },
            ],
            app_parameters=[],
            warnings=[],
        )
        assert (await post(console, "recommendations", body)).status_code == 200


async def test_unapplied_and_platform_template_records_are_not_sources(
    console: Console, model: FakeModel
) -> None:
    await console.login()
    engine = console.app.state.ai_service.infra.deployment
    _, planned = engine.create(
        {
            "org": {"id": org_id(USER, "goods"), "name": "goods"},
            "cloud": "onprem",
            "app": {"image": CONFIG["clouds"]["onprem"]["images"]["chiikawa-groupbuy"]},
            "target": {"nodeId": "lab"},
            "mode": "plan",
        }
    )
    drain(engine)
    assert (
        await post(console, "chat", {"message": "hi", "deploymentId": planned["environmentId"]})
    ).status_code == 404
    record = engine.store.environment(source_id(console))
    engine.store.update_environment(
        record["id"], record["updated_at"], template="platform-control-plane"
    )
    assert (
        await post(
            console,
            "recommendations",
            {"sourceDeploymentId": record["id"], "targetEnvironment": "gcp"},
        )
    ).status_code == 404
    assert not model.calls


@pytest.mark.parametrize("seconds", [29, 86401])
async def test_analysis_window_bounds(console: Console, model: FakeModel, seconds: int) -> None:
    await console.login()
    body = analysis_body(console)
    body["timeRange"]["start"] = (
        console.world.clock.now() - timedelta(seconds=seconds)
    ).isoformat()
    assert (await post(console, "analyses", body)).status_code == 422
    assert not model.calls
