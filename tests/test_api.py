"""The console's contract: paths, login, CSRF, idempotency, and who may call what."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from control_plane.auth import AuthService, safe_return_path
from control_plane.config import Settings
from control_plane.domain.models import ActorState
from control_plane.main import create_api
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
    assert detail["workflow"]["status"] == "PLANNED"
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
    assert detail["allowedCommands"] == ["APPROVE", "ABORT"]
    assert detail["hold"]["desired"] == "RUN"


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
    workflow = created["workflow"]
    url = f"/v1/workflows/{workflow['id']}/commands"

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


async def test_timeline_records_who_did_what_and_pages(console: Console, world: World) -> None:
    await console.login()
    workflow = (await console.create())["workflow"]
    await console.http.post(
        f"/v1/workflows/{workflow['id']}/commands",
        json={"command": "APPROVE", "expectedVersion": workflow["version"]},
        headers=console.change_headers(),
    )
    for _ in range(10):
        await world.tick()

    first = (await console.http.get(f"/v1/workflows/{workflow['id']}/events?limit=2")).json()
    assert [event["type"] for event in first["events"]] == ["WORKFLOW_CREATED", "APPROVE"]
    assert first["events"][1]["actor"] == "hyunsu"
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


async def test_provisioning_is_reserved(console: Console) -> None:
    await console.login()
    response = await console.http.get("/v1/provisioning/migrations/wf-1")
    assert response.status_code == 501
    assert response.json()["code"] == "NOT_IMPLEMENTED"


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


async def test_personal_workspace_hides_other_users_app_and_workflow(console: Console) -> None:
    from datetime import timedelta

    from control_plane.api.dependencies import current_session
    from control_plane.domain.models import Session, User

    await console.login()
    created = await console.create()
    workflow = created["workflow"]

    async def stranger() -> Session:
        now = console.world.clock.now()
        return Session(
            user=User(id="github:999", github_id=999, login="other"),
            csrf_token=console.csrf,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

    console.app.dependency_overrides[current_session] = stranger
    assert (await console.http.get("/v1/applications")).json() == []
    assert (await console.http.get("/v1/workflows")).json() == []
    for suffix in ("", "/events"):
        assert (
            await console.http.get(f"/v1/workflows/{workflow['id']}{suffix}")
        ).status_code == 404
    response = await console.http.post(
        f"/v1/workflows/{workflow['id']}/commands",
        json={"command": "APPROVE", "expectedVersion": workflow["version"]},
        headers=console.change_headers(),
    )
    assert response.status_code == 404
