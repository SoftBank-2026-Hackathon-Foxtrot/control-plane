"""The console's contract: paths, login, CSRF, idempotency, and who may call what."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from control_plane.adapters.client import (
    AdapterClient,
    AdapterUnreachable,
    MigrationView,
    OperationView,
)
from control_plane.auth import AuthService, safe_return_path
from control_plane.config import Settings
from control_plane.domain.models import ActorState, ResourceKind, WorkflowStatus
from control_plane.errors import ApiError
from control_plane.main import create_api
from control_plane.onprem_database import OnpremDatabase
from tests.conftest import SETTINGS, World

ORIGIN = "https://platform.test"
ALLOWED_GITHUB_ID = 4242


def github(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/login/oauth/access_token":
        form = parse_qs(request.content.decode())
        assert form["code_verifier"], "PKCE must reach the token exchange"
        code = form["code"][0]
        return httpx.Response(200, json={} if code == "bad" else {"access_token": f"t-{code}"})
    assert request.url.path == "/user"
    is_stranger = request.headers["authorization"].endswith("stranger")
    return httpx.Response(
        200,
        json={
            "id": 1 if is_stranger else ALLOWED_GITHUB_ID,
            "login": "stranger" if is_stranger else "hyunsu",
            "avatar_url": "https://avatars.test/1",
        },
    )


class Console:
    def __init__(self, world: World, settings: Settings) -> None:
        self.world = world
        context = world.platform.context
        github_http = httpx.AsyncClient(transport=httpx.MockTransport(github))
        object.__setattr__(
            context,
            "auth",
            AuthService(
                world.repo,
                world.clock,
                github_http,
                client_id="client",
                client_secret="secret",
                callback_url=f"{ORIGIN}/api/auth/github/callback",
                allowed_github_ids=[ALLOWED_GITHUB_ID],
                session_hours=8,
            ),
        )
        object.__setattr__(context, "settings", settings)
        self.app = create_api(world.platform, run_loops=False)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://platform.test",
            follow_redirects=False,
        )
        self.csrf = ""
        self._key = 0

    def key(self) -> str:
        self._key += 1
        return f"key-{self._key}"

    async def login(self, code: str = "good") -> httpx.Response:
        start = await self.http.get("/auth/github/start?returnTo=/applications")
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        response = await self.http.get(f"/auth/github/callback?code={code}&state={state}")
        if response.status_code == 303:
            self.csrf = (await self.http.get("/v1/me")).json()["csrfToken"]
        return response

    def change_headers(self, **extra: str) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
            "Idempotency-Key": self.key(),
            **extra,
        }

    async def create(self) -> dict[str, Any]:
        response = await self.http.post(
            "/v1/workflows",
            json={"sourceEnv": "aws", "targetEnv": "gcp"},
            headers=self.change_headers(),
        )
        assert response.status_code == 201, response.text
        return dict(response.json())


@pytest.fixture
async def console(world: World) -> AsyncIterator[Console]:
    instance = Console(world, SETTINGS)
    yield instance
    await instance.http.aclose()


async def test_health_has_the_shape_the_frontend_validates(console: Console) -> None:
    response = await console.http.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


async def test_everything_else_needs_a_session(console: Console) -> None:
    for path in ("/v1/me", "/v1/workspaces", "/v1/applications", "/v1/workflows"):
        response = await console.http.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "UNAUTHENTICATED"


async def test_login_flow_issues_a_session_and_returns_inside_the_console(
    console: Console,
) -> None:
    response = await console.login()
    assert response.status_code == 303
    assert response.headers["location"] == f"{ORIGIN}/applications"
    cookie = response.headers["set-cookie"]
    assert "foxtrot_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    me = await console.http.get("/v1/me")
    assert me.headers["cache-control"] == "no-store"
    body = me.json()
    assert body["user"]["githubId"] == ALLOWED_GITHUB_ID
    assert body["user"]["login"] == "hyunsu"
    assert {"expiresAt", "csrfToken"} <= set(body)
    assert "token" not in str(body).replace("csrfToken", "")
    workspaces = await console.http.get("/v1/workspaces")
    assert workspaces.json()[0]["role"] == "owner"


async def test_secure_cookie_names_in_production(world: World) -> None:
    settings = Settings(
        environment="production",
        public_url=ORIGIN,
        allowed_origins=[ORIGIN],
        _env_file=None,  # type: ignore[call-arg]
    )
    console = Console(world, settings)
    response = await console.login()
    cookie = next(
        value for value in response.headers.get_list("set-cookie") if "foxtrot_session" in value
    )
    assert cookie.startswith("__Host-foxtrot_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie


async def test_only_listed_github_accounts_may_enter(console: Console) -> None:
    response = await console.login("stranger")
    assert response.status_code == 403
    assert response.json()["code"] == "NOT_ALLOWED"
    assert (await console.http.get("/v1/me")).status_code == 401


async def test_a_login_state_works_once(console: Console) -> None:
    start = await console.http.get("/auth/github/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert (
        await console.http.get(f"/auth/github/callback?code=good&state={state}")
    ).status_code == 303
    again = await console.http.get(f"/auth/github/callback?code=good&state={state}")
    assert again.json()["code"] == "LOGIN_EXPIRED"


async def test_a_login_must_finish_in_the_browser_that_started_it(console: Console) -> None:
    start = await console.http.get("/auth/github/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    victim = httpx.AsyncClient(transport=httpx.ASGITransport(app=console.app), base_url=ORIGIN)
    response = await victim.get(f"/auth/github/callback?code=good&state={state}")
    assert response.json()["code"] == "LOGIN_BROWSER_MISMATCH"
    await victim.aclose()


async def test_an_expired_login_request_is_refused(console: Console, world: World) -> None:
    start = await console.http.get("/auth/github/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    world.clock.advance(6 * 60)
    response = await console.http.get(f"/auth/github/callback?code=good&state={state}")
    assert response.json()["code"] == "LOGIN_EXPIRED"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/applications", "/applications"),
        ("https://evil.test", "/"),
        ("//evil.test", "/"),
        ("/\\evil.test", "/"),
        (None, "/"),
    ],
)
def test_return_paths_stay_inside_the_console(path: str | None, expected: str) -> None:
    assert safe_return_path(path) == expected


async def test_a_session_ends_at_expiry_and_at_logout(console: Console, world: World) -> None:
    await console.login()
    world.clock.advance(8 * 3600 + 1)
    assert (await console.http.get("/v1/me")).status_code == 401

    await console.login()
    response = await console.http.post("/auth/logout", headers=console.change_headers())
    assert response.status_code == 204
    assert (await console.http.get("/v1/me")).status_code == 401


async def test_changes_need_csrf_token_and_allowed_origin(console: Console) -> None:
    await console.login()
    body = {"sourceEnv": "aws", "targetEnv": "gcp"}
    no_token = await console.http.post(
        "/v1/workflows", json=body, headers={"Origin": ORIGIN, "Idempotency-Key": "a"}
    )
    assert no_token.json()["code"] == "CSRF_REJECTED"
    wrong_origin = await console.http.post(
        "/v1/workflows",
        json=body,
        headers={
            "Origin": "https://evil.test",
            "X-CSRF-Token": console.csrf,
            "Idempotency-Key": "a",
        },
    )
    assert wrong_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
    no_origin = await console.http.post(
        "/v1/workflows",
        json=body,
        headers={"X-CSRF-Token": console.csrf, "Idempotency-Key": "a"},
    )
    assert no_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"


async def test_creating_a_workflow_returns_the_whole_picture(console: Console) -> None:
    await console.login()
    detail = await console.create()
    assert detail["workflow"]["status"] == "RUNNING"
    assert detail["workflow"]["createdBy"] == "hyunsu"
    assert [stage["kind"] for stage in detail["stages"]] == [
        "PREPARE",
        "SYNC",
        "OBJECT_CUTOVER",
        "CANARY",
        "REDIS_CUTOVER",
        "DATABASE_CUTOVER",
        "TRAFFIC",
        "FINALIZE",
    ]
    assert detail["allowedCommands"] == ["HALT"]
    assert detail["hold"]["desired"] == "RUN"


@pytest.mark.parametrize("cloud_source,cloud_target", [("aws", "gcp"), ("gcp", "aws")])
@pytest.mark.parametrize("cloud_first", [False, True])
async def test_onprem_planning_is_independent_of_cloud_migrations(
    console: Console, cloud_source: str, cloud_target: str, cloud_first: bool
) -> None:
    world = console.world
    settings = world.platform.context.settings.model_copy(update={"onprem_planning": True})
    object.__setattr__(world.platform.context, "settings", settings)
    await console.login()
    apps = (await console.http.get("/v1/applications")).json()
    assert [env["name"] for env in apps[0]["environments"]] == ["aws", "gcp", "onprem"]
    assert "onprem" not in world.seed.environments
    hold_before = await world.repo.hold(world.seed.scope)

    async def cloud() -> str:
        result = await console.http.post(
            "/v1/workflows",
            json={"sourceEnv": cloud_source, "targetEnv": cloud_target},
            headers=console.change_headers(),
        )
        assert result.status_code == 201
        return str(result.json()["workflow"]["id"])

    cloud_id = await cloud() if cloud_first else None
    headers = console.change_headers()
    args = {"json": {"sourceEnv": "aws", "targetEnv": "onprem"}, "headers": headers}
    response = await console.http.post("/v1/workflows", **args)
    assert response.status_code == 201
    plan = response.json()
    assert plan["workflow"]["status"] == "PLANNED" and plan["allowedCommands"] == ["ABORT"]
    assert (await console.http.post("/v1/workflows", **args)).json() == plan
    assert await world.repo.active_workflow_id() == cloud_id
    cloud_id = cloud_id or await cloud()
    wid = plan["workflow"]["id"]
    assert await world.repo.workflow(wid) is None
    assert {row["id"] for row in (await console.http.get("/v1/workflows")).json()} == {
        wid,
        cloud_id,
    }
    assert (await console.http.get(f"/v1/workflows/{wid}")).json() == plan
    found = await console.http.get(f"/v1/workflows/{wid}/events")
    assert found.json()["events"][0]["type"] == "WORKFLOW_CREATED"
    assert (await console.http.get(f"/v1/workflows/{wid}/observations")).json()[
        "observations"
    ] == []
    with pytest.raises(ApiError, match="on-prem plan not found"):
        await world.platform.context.onprem_plans.get(wid, "another-user")
    for action, code in (("APPROVE", 409), ("ABORT", 200)):
        response = await console.http.post(
            f"/v1/workflows/{wid}/commands",
            json={"command": action, "expectedVersion": plan["workflow"]["version"]},
            headers=console.change_headers(),
        )
        assert response.status_code == code
    assert response.json()["workflow"]["status"] == "ABORTED"
    assert await world.repo.active_workflow_id() == cloud_id
    assert await world.repo.hold(world.seed.scope) == hold_before
    assert (await world.run_to_end(cloud_id)).status.value == "COMPLETED"


async def test_onprem_database_request_is_planned_without_dispatch(console: Console) -> None:
    world = console.world
    settings = world.platform.context.settings.model_copy(update={"onprem_planning": True})
    object.__setattr__(world.platform.context, "settings", settings)
    await console.login()
    owner = (await console.http.get("/v1/me")).json()["user"]["id"]
    ref = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test/"
    source = {
        "provider": "AWS_RDS",
        "providerInstance": "arn:aws:rds:ap-northeast-2:123456789012:db:lab",
        "host": "source.example",
        "database": "app",
        "writerRole": "app",
        "secretRef": ref + "rds",
    }
    target = {
        "provider": "SELF_MANAGED",
        "host": "vm.example",
        "database": "appdb",
        "writerRole": "app",
        "secretRef": ref + "vm",
    }

    async def create(src: str, dst: str, node: dict[str, str]) -> httpx.Response:
        return await console.http.post(
            "/v1/workflows",
            headers=console.change_headers(),
            json={
                "sourceEnv": src,
                "targetEnv": dst,
                "resources": {"DATABASE": {"source": source, "target": node}},
            },
        )

    before = await world.repo.hold(world.seed.scope)
    for invalid in (
        target | {"writerRole": "other"},
        target | {"secretRef": "literal-value"},
        target | {"provider": "GCP_CLOUDSQL"},
    ):
        assert (await create("aws", "onprem", invalid)).status_code == 422
    assert (await create("onprem", "aws", target)).status_code == 422
    response = await create("aws", "onprem", target)
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["allowedCommands"] == ["ABORT"]
    assert plan["resources"][0]["migrationId"] is None
    wid = plan["workflow"]["id"]
    stored = await world.platform.context.onprem_plans.get(wid, owner)
    request = stored.database_request
    assert request == {
        "kind": "DATABASE",
        "resourceId": "database",
        "workflowId": wid,
        "profile": plan["resources"][0]["details"]["profile"],
        "policy": {"rollbackMode": "BEFORE_TARGET_WRITE", "maxQuiesceSeconds": 60},
        "source": source | {"port": 5432},
        "target": target | {"port": 5432},
    }
    assert request["profile"] == "postgres-rds-onprem-logical-pgbouncer-v1"
    assert await world.repo.active_workflow_id() is None
    assert await world.repo.hold(world.seed.scope) == before


async def test_onprem_approvals_replay_lost_prepare_then_start_forward_sync(
    console: Console,
) -> None:
    context = console.world.platform.context
    await console.login()
    role = {"writerRole": "app", "database": "app"}
    secret = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test/"
    pair = OnpremDatabase.model_validate(
        {
            "bindingRef": "binding:onprem-demo",
            "source": {
                **role,
                "provider": "AWS_RDS",
                "host": "rds.example",
                "secretRef": secret + "source",
                "providerInstance": "arn:aws:rds:ap-northeast-2:123456789012:db:lab",
            },
            "target": {
                **role,
                "provider": "SELF_MANAGED",
                "host": "vm.example",
                "secretRef": secret + "target",
            },
        }
    )
    object.__setattr__(
        context,
        "settings",
        context.settings.model_copy(update={"onprem_planning": True, "onprem_database": pair}),
    )
    db = AsyncMock(spec=AdapterClient)
    context.workflows.adapters = {**context.workflows.adapters, ResourceKind.DATABASE: db}
    created = MigrationView({"migrationId": "m-test", "revision": 1, "lifecycle": "CREATED"})
    prepared = MigrationView(
        {
            "migrationId": "m-test",
            "revision": 3,
            "lifecycle": "PREPARED",
            "allowedActions": ["START_SYNC"],
        }
    )
    db.create_migration.return_value = created
    db.act.side_effect = [AdapterUnreachable("lost ACK"), "prepare-op", "sync-op"]
    db.migration.return_value = prepared
    db.operation.return_value = OperationView("prepare-op", "SUCCEEDED", None, {})
    hold = await console.world.repo.hold(console.world.seed.scope)
    body = {"sourceEnv": "aws", "targetEnv": "onprem"}
    wrong = pair.model_dump(by_alias=True, exclude={"binding_ref"})
    wrong["target"]["host"] = "unregistered.example"
    foreign = (
        await console.http.post(
            "/v1/workflows",
            headers=console.change_headers(),
            json={**body, "resources": {"DATABASE": wrong}},
        )
    ).json()
    assert foreign["allowedCommands"] == ["ABORT"]
    refused = await console.http.post(
        f"/v1/workflows/{foreign['workflow']['id']}/commands",
        headers=console.change_headers(),
        json={"command": "APPROVE", "expectedVersion": 1},
    )
    assert refused.status_code == 409
    db.create_migration.assert_not_awaited()
    plan = (
        await console.http.post("/v1/workflows", json=body, headers=console.change_headers())
    ).json()
    wid = plan["workflow"]["id"]
    assert plan["allowedCommands"] == ["APPROVE", "ABORT"]

    async def command(action: str, version: int) -> httpx.Response:
        return await console.http.post(
            f"/v1/workflows/{wid}/commands",
            headers=console.change_headers(),
            json={"command": action, "expectedVersion": version},
        )

    assert (await command("APPROVE", 1)).status_code == 503
    after_loss = (await console.http.get(f"/v1/workflows/{wid}")).json()
    assert after_loss["allowedCommands"] == ["APPROVE"]
    version = after_loss["workflow"]["version"]
    assert (await command("ABORT", version)).status_code == 409
    prepared_plan = (await command("APPROVE", version)).json()
    assert db.act.await_args_list[0] == db.act.await_args_list[1]
    assert prepared_plan["resources"][0]["migrationId"] == "m-test"
    assert prepared_plan["allowedCommands"] == ["APPROVE"]
    synced = (await command("APPROVE", prepared_plan["workflow"]["version"])).json()
    assert synced["allowedCommands"] == [] and synced["workflow"]["currentStage"] == 1
    db.act.assert_awaited_with(
        "m-test", f"{wid}:START_SYNC", {"action": "START_SYNC", "expectedRevision": 3}
    )
    db.create_migration.assert_awaited_once()
    assert db.create_migration.await_args.args[1]["bindingRef"] == "binding:onprem-demo"
    assert await console.world.repo.active_workflow_id() is None
    assert await console.world.repo.hold(console.world.seed.scope) == hold


async def test_resource_details_expose_adapter_rollback_evidence(
    console: Console, world: World
) -> None:
    await console.login()
    detail = await console.create()
    workflow_id = detail["workflow"]["id"]
    resource = await world.repo.resource(workflow_id, ResourceKind.DATABASE)
    status = {
        "rollback": {"method": "REVERSE_SYNC", "reasons": ["REVERSE_LAG"]},
        "readiness": {"rollback": {"result": "UNKNOWN", "reasons": ["REVERSE_LAG"]}},
        "details": {"database": {"reverseReplication": {"status": "SYNCING", "lagBytes": 512}}},
    }
    await world.repo.save_resource(workflow_id, resource.model_copy(update={"last_status": status}))
    response = await console.http.get(f"/v1/workflows/{workflow_id}")
    assert response.status_code == 200
    database = next(r for r in response.json()["resources"] if r["kind"] == "DATABASE")
    for field in ("rollback", "readiness", "details"):
        assert database[field] == status[field]


async def test_one_migration_at_a_time(console: Console) -> None:
    await console.login()
    first = await console.create()
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "gcp", "targetEnv": "aws"},
        headers=console.change_headers(),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WORKFLOW_ACTIVE"
    assert response.json()["blockers"] == [first["workflow"]["id"]]


@pytest.mark.parametrize(
    ("source", "target", "code"),
    [
        ("aws", "aws", "SAME_ENVIRONMENT"),
        ("aws", "mars", "UNKNOWN_ENVIRONMENT"),
    ],
)
async def test_invalid_plans_are_refused(
    console: Console, source: str, target: str, code: str
) -> None:
    await console.login()
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": source, "targetEnv": target},
        headers=console.change_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == code


async def test_a_plan_is_refused_if_an_adapter_does_not_offer_the_profile(
    console: Console, world: World
) -> None:
    await console.login()
    world.redis.capabilities = lambda: {"profiles": [{"profile": "something-else"}]}  # type: ignore[method-assign]
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "aws", "targetEnv": "gcp"},
        headers=console.change_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_PROFILE"
    assert await world.repo.active_workflow_id() is None


async def test_a_plan_is_refused_if_an_adapter_is_down(console: Console, world: World) -> None:
    await console.login()
    world.db.unreachable = True
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "aws", "targetEnv": "gcp"},
        headers=console.change_headers(),
    )
    assert response.status_code == 503
    assert response.json()["code"] == "ADAPTER_UNAVAILABLE"


async def test_the_same_key_has_one_effect(console: Console) -> None:
    await console.login()
    headers = console.change_headers()
    body = {"sourceEnv": "aws", "targetEnv": "gcp"}
    first = await console.http.post("/v1/workflows", json=body, headers=headers)
    second = await console.http.post("/v1/workflows", json=body, headers=headers)
    assert second.status_code == 201
    assert first.json()["workflow"]["id"] == second.json()["workflow"]["id"]
    other = await console.http.post(
        "/v1/workflows", json={"sourceEnv": "gcp", "targetEnv": "aws"}, headers=headers
    )
    assert other.status_code == 409
    assert other.json()["code"] == "IDEMPOTENCY_CONFLICT"


async def test_a_refused_request_can_be_sent_again_with_the_same_key(console: Console) -> None:
    await console.login()
    headers = console.change_headers()
    bad = {"sourceEnv": "aws", "targetEnv": "aws"}
    assert (await console.http.post("/v1/workflows", json=bad, headers=headers)).status_code == 422
    again = await console.http.post("/v1/workflows", json=bad, headers=headers)
    assert again.status_code == 422


async def test_changes_need_an_idempotency_key(console: Console) -> None:
    await console.login()
    response = await console.http.post(
        "/v1/workflows",
        json={"sourceEnv": "aws", "targetEnv": "gcp"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": console.csrf},
    )
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_commands_check_version_and_state(console: Console, world: World) -> None:
    await console.login()
    created = await console.create()
    workflow_id = created["workflow"]["id"]
    await world.until(workflow_id, WorkflowStatus.WAITING_APPROVAL)
    workflow = (await console.http.get(f"/v1/workflows/{workflow_id}")).json()["workflow"]
    url = f"/v1/workflows/{workflow_id}/commands"

    stale = await console.http.post(
        url,
        json={"command": "APPROVE", "expectedVersion": workflow["version"] + 5},
        headers=console.change_headers(),
    )
    assert stale.json()["code"] == "VERSION_CONFLICT"

    wrong = await console.http.post(
        url,
        json={"command": "RESUME", "expectedVersion": workflow["version"]},
        headers=console.change_headers(),
    )
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "INVALID_STATE"
    assert wrong.json()["blockers"] == ["APPROVE", "ABORT"]

    approved = await console.http.post(
        url,
        json={"command": "APPROVE", "expectedVersion": workflow["version"]},
        headers=console.change_headers(),
    )
    assert approved.status_code == 200
    assert approved.json()["workflow"]["status"] == "RUNNING"
    assert approved.json()["allowedCommands"] == ["HALT"]

    complete = await world.until(workflow_id, WorkflowStatus.CUTOVER_COMPLETE)
    detail = (await console.http.get(f"/v1/workflows/{workflow_id}")).json()
    assert detail["allowedCommands"] == ["ROLLBACK", "FINALIZE"]
    refused = await console.http.post(
        url,
        json={"command": "APPROVE", "expectedVersion": complete.version},
        headers=console.change_headers(),
    )
    assert refused.status_code == 409
    finalized = await console.http.post(
        url,
        json={"command": "FINALIZE", "expectedVersion": complete.version},
        headers=console.change_headers(),
    )
    assert finalized.status_code == 200
    assert finalized.json()["workflow"]["finalizeRequested"]


async def test_timeline_records_who_did_what_and_pages(console: Console, world: World) -> None:
    await console.login()
    workflow = (await console.create())["workflow"]
    current = await world.until(workflow["id"], WorkflowStatus.WAITING_APPROVAL)
    workflow["version"] = current.version
    await console.http.post(
        f"/v1/workflows/{workflow['id']}/commands",
        json={"command": "APPROVE", "expectedVersion": workflow["version"]},
        headers=console.change_headers(),
    )
    for _ in range(10):
        await world.tick()

    first = (await console.http.get(f"/v1/workflows/{workflow['id']}/events?limit=2")).json()
    assert [event["type"] for event in first["events"]] == ["WORKFLOW_CREATED", "STAGE_STARTED"]
    events = (await console.http.get(f"/v1/workflows/{workflow['id']}/events?limit=100")).json()
    assert (
        next(event for event in events["events"] if event["type"] == "APPROVE")["actor"] == "hyunsu"
    )
    assert first["nextCursor"]
    second = (
        await console.http.get(
            f"/v1/workflows/{workflow['id']}/events?limit=2&after={first['nextCursor']}"
        )
    ).json()
    assert second["events"][0]["cursor"] > first["events"][1]["cursor"]


async def test_workflow_detail_shows_resources_and_actors(console: Console, world: World) -> None:
    await console.login()
    workflow = (await console.create())["workflow"]
    await world.at_stage(workflow["id"], 2)
    detail = (await console.http.get(f"/v1/workflows/{workflow['id']}")).json()

    kinds = {resource["kind"]: resource for resource in detail["resources"]}
    assert set(kinds) == {"DATABASE", "REDIS", "OBJECT_STORAGE"}
    assert kinds["REDIS"]["lifecycle"] == "READY"
    assert kinds["REDIS"]["route"] == "SOURCE"
    assert {actor["actorId"] for actor in detail["hold"]["actors"]} == set(world.seed.all_actors())
    assert detail["stages"][0]["status"] == "PASSED"


async def test_applications_come_from_the_seed(console: Console) -> None:
    await console.login()
    [application] = (await console.http.get("/v1/applications")).json()
    assert application["id"] == "goods"
    assert {env["name"] for env in application["environments"]} == {"aws", "gcp"}


async def test_provisioning_requires_configuration(console: Console) -> None:
    await console.login()
    response = await console.http.get("/v1/provisioning/migrations/wf-1")
    assert response.status_code == 503
    assert response.json()["code"] == "DEPLOYMENT_NOT_CONFIGURED"


async def test_heartbeat_needs_the_workload_token_and_known_names(console: Console) -> None:
    url = "/internal/v1/workloads/scope:goods/heartbeat"
    body = {
        "actorId": "goods-aws-api",
        "instanceId": "host-1",
        "generation": 0,
        "state": ActorState.RUNNING.value,
        "inFlight": 0,
    }
    assert (await console.http.post(url, json=body)).status_code == 401
    wrong = await console.http.post(url, json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401

    headers = {"Authorization": "Bearer workload-token"}
    ok = await console.http.post(url, json=body, headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {
        "desired": "RUN",
        "generation": 0,
        "objectMigrationId": None,
        "pollAfterMs": 5000,
        "redisRoute": "PROXY",
        "redisEndpoint": None,
        "redisRouteGeneration": 0,
    }
    unknown = await console.http.post(
        url, json={**body, "actorId": "someone-else"}, headers=headers
    )
    assert unknown.json()["code"] == "UNKNOWN_ACTOR"
    other_scope = await console.http.post(
        "/internal/v1/workloads/scope:other/heartbeat", json=body, headers=headers
    )
    assert other_scope.status_code == 404


async def test_auth_disabled_is_refused_outside_local() -> None:
    with pytest.raises(ValueError, match="auth_disabled"):
        Settings(environment="production", auth_disabled=True, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="memory"):
        Settings(environment="production", dynamodb_endpoint="memory", _env_file=None)  # type: ignore[call-arg]


def production() -> Settings:
    return Settings(
        environment="production",
        public_url=ORIGIN,
        allowed_origins=[ORIGIN],
        _env_file=None,  # type: ignore[call-arg]
    )


async def test_declining_at_github_returns_to_the_login_page(console: Console) -> None:
    response = await console.http.get("/auth/github/callback?error=access_denied")
    assert response.status_code == 303
    assert response.headers["location"] == f"{ORIGIN}/login?error=login_cancelled"


async def test_logging_in_again_retires_the_old_session(console: Console) -> None:
    await console.login()
    old = console.http.cookies.get("foxtrot_session")
    await console.login()
    assert old != console.http.cookies.get("foxtrot_session")
    stale = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=console.app),
        base_url=ORIGIN,
        cookies={"foxtrot_session": old or ""},
    )
    assert (await stale.get("/v1/me")).status_code == 401
    await stale.aclose()


async def test_clearing_a_host_cookie_has_the_attributes_browsers_require(world: World) -> None:
    console = Console(world, production())
    await console.login()
    response = await console.http.post("/auth/logout", headers=console.change_headers())
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-foxtrot_session=")
    assert "Secure" in cookie and "Path=/" in cookie


async def test_a_non_ascii_token_is_a_refusal_not_a_crash(console: Console) -> None:
    await console.login()
    headers = {**console.change_headers(), "X-CSRF-Token": "토큰".encode()}
    response = await console.http.post(
        "/v1/workflows", json={"sourceEnv": "aws", "targetEnv": "gcp"}, headers=headers
    )
    assert response.status_code == 403


async def test_api_documents_are_not_public_in_production(world: World) -> None:
    console = Console(world, production())
    assert (await console.http.get("/docs")).status_code == 404
    assert (await console.http.get("/openapi.json")).status_code == 404


async def test_login_off_must_stay_on_this_machine() -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        Settings(environment="local", auth_disabled=True, host="0.0.0.0", _env_file=None)  # type: ignore[call-arg]


async def test_preparation_reports_need_a_separate_token_and_preserve_original_time(
    console: Console, world: World
) -> None:
    await console.login()
    workflow_id = (await console.create())["workflow"]["id"]
    url = f"/internal/workflows/{workflow_id}/preparation"
    object.__setattr__(world.platform.context, "observation_token", "report-token")
    body = {
        "resource": "INFRA",
        "data": {
            "workflowId": workflow_id,
            "stage": "PLAN",
            "status": "SUCCEEDED",
            "observedAt": world.clock.now().isoformat(),
            "changes": {"create": 1},
            "secrets": {"token": "secret"},
        },
    }
    assert (await console.http.post(url, json=body)).status_code == 401
    assert (
        await console.http.post(url, json=body, headers={"Authorization": "Bearer workload-token"})
    ).status_code == 401
    response = await console.http.post(
        url, json=body, headers={"Authorization": "Bearer report-token"}
    )
    assert response.status_code == 410
    from tests.test_otel import deliver

    assert (await deliver(world, body["data"], kind="INFRA", module="iac")).status_code == 200
    detail = (await console.http.get(f"/v1/workflows/{workflow_id}")).json()
    assert "secret" not in str(detail["preparation"])
    assert detail["preparation"]["INFRA"]["data"]["observedAt"] == body["data"]["observedAt"]
    world.clock.advance(20)
    detail = (await console.http.get(f"/v1/workflows/{workflow_id}")).json()
    assert detail["preparation"]["INFRA"]["observation"]["freshness"] == "STALE"
    response = await console.http.get(f"/v1/workflows/{workflow_id}/observations?limit=1")
    assert response.status_code == 200
    assert len(response.json()["observations"]) == 1
    assert (
        await console.http.get(f"/v1/workflows/{workflow_id}/observations?limit=501")
    ).status_code == 422
    assert (
        await console.http.get(
            f"/v1/workflows/{workflow_id}/observations?since=2020-01-01T00:00:00Z"
        )
    ).status_code == 422
