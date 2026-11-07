from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from control_plane.errors import ApiError
from control_plane.security_review.models import Change, ReviewConfig, without_extra_grant
from control_plane.security_review.provider import FixtureProvider
from control_plane.security_review.service import ReviewService
from tests.conftest import SETTINGS, World
from tests.test_api import Console

CONFIG = ReviewConfig(
    application="goods",
    allowed_user_ids=["github:4242"],
    aws_role="goods-app",
    aws_bucket="goods-aws",
    gcp_bucket="goods-gcp",
    extra_bucket="goods-security-demo",
    gcp_principal="serviceAccount:goods@app-project.iam.gserviceaccount.com",
    mode="fixture",
)


async def ready() -> bool:
    return True


def setup(world: World) -> tuple[ReviewService, FixtureProvider]:
    provider = FixtureProvider(CONFIG)
    return ReviewService(CONFIG, provider, world.table, world.clock, ready), provider


async def run(review: ReviewService, action: str) -> dict:
    state = await review.load()
    return await review.change(
        action,
        Change(revision=state["revision"], plan_id=state["plan"]["id"] if state["plan"] else None),
        "reviewer",
    )


async def test_real_service_flow_preserves_required_access_and_other_principal(
    world: World,
) -> None:
    review, provider = setup(world)
    original = deepcopy(provider.data)
    gap = await run(review, "scan")
    assert gap["phase"] == "gap" and gap["appReady"]
    assert "snapshot" not in gap
    plan = await run(review, "plan")
    assert plan["plan"]["retainBucket"] == CONFIG.gcp_bucket
    applied = await run(review, "apply")
    assert applied["phase"] == "applied" and not applied["uiCanContinue"]
    assert provider.data["source"] == original["source"]
    assert provider.data["target"] == original["target"]
    assert provider.data["extra"]["bindings"][0]["members"] == [
        "serviceAccount:other@demo-project.iam.gserviceaccount.com"
    ]
    verified = await run(review, "verify")
    assert verified["uiCanContinue"] and provider.writes == 1
    assert len(verified["events"]) == 4
    recreated = ReviewService(CONFIG, provider, world.table, world.clock, ready)
    assert (await recreated.load())["phase"] == "verified"
    assert await world.table.get("WORKFLOWS", "ACTIVE") is None


async def test_stale_revision_and_plan_never_write(world: World) -> None:
    review, provider = setup(world)
    await run(review, "scan")
    await run(review, "plan")
    with pytest.raises(ApiError, match="바뀌었습니다"):
        await review.change("apply", Change(revision=0, plan_id="invalid"), "actor")
    provider.data["extra"]["etag"] = "someone-else-changed"
    failed = await run(review, "apply")
    assert failed["phase"] == "error" and not failed["uiCanContinue"]
    assert provider.writes == 0


async def test_required_access_or_unknown_source_cannot_generate_plan(world: World) -> None:
    review, provider = setup(world)
    provider.data["source"]["Statement"][0]["Resource"] = "*"
    result = await run(review, "scan")
    assert result["phase"] == "unknown"
    with pytest.raises(ApiError):
        await run(review, "plan")
    provider.data = FixtureProvider(CONFIG).data
    provider.data["target"]["bindings"] = []
    assert (await run(review, "scan"))["phase"] == "unknown"
    assert provider.writes == 0


async def test_verify_requires_actual_app_readiness_and_expires(world: World) -> None:
    review, provider = setup(world)
    provider.data["extra"]["bindings"] = []
    await run(review, "scan")

    async def unavailable() -> bool:
        return False

    review.probe = unavailable
    assert not (await run(review, "verify"))["uiCanContinue"]
    review.probe = ready
    result = await run(review, "verify")
    assert result["uiCanContinue"]
    world.clock.advance(601)
    assert not review.view(await review.load())["uiCanContinue"]


async def test_concurrent_scans_have_one_owner(world: World) -> None:
    review, _ = setup(world)
    outcomes = await asyncio.gather(
        review.change("scan", Change(revision=0), "a"),
        review.change("scan", Change(revision=0), "b"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ApiError) for r in outcomes) == 1


@pytest.mark.parametrize(
    "condition,role", [(True, "roles/storage.objectUser"), (False, "roles/owner")]
)
def test_unsupported_grants_are_not_silently_removed(condition: bool, role: str) -> None:
    policy = FixtureProvider(CONFIG).data["extra"]
    policy["bindings"][0]["role"] = role
    if condition:
        policy["bindings"][0]["condition"] = {"expression": "true"}
    with pytest.raises(ValueError):
        without_extra_grant(policy, CONFIG.gcp_principal)


async def test_failure_does_not_leak_cloud_error_or_pass(world: World) -> None:
    review, provider = setup(world)
    await run(review, "scan")

    def fail() -> dict:
        raise RuntimeError("SECRET_CREDENTIAL")

    provider.collect = fail  # type: ignore[method-assign]
    result = await run(review, "scan")
    assert result["phase"] == "error" and "SECRET_CREDENTIAL" not in str(result)
    assert not result["uiCanContinue"]


async def test_auth_csrf_target_allowlist_and_existing_routes(world: World) -> None:
    console = Console(world, SETTINGS)
    review, _ = setup(world)
    console.app.state.security_review = review
    assert (await console.http.get("/v1/security-review")).status_code == 401
    await console.login()
    user_id = (await console.http.get("/v1/me")).json()["user"]["id"]
    review.config = CONFIG.model_copy(update={"allowed_user_ids": [user_id]})
    assert (await console.http.get("/v1/security-review")).status_code == 200
    assert (
        await console.http.post("/v1/security-review/scan", json={"revision": 0})
    ).status_code == 403
    result = await console.http.post(
        "/v1/security-review/scan", json={"revision": 0}, headers=console.change_headers()
    )
    assert result.status_code == 200 and result.json()["phase"] == "gap"
    # No privilege to choose an arbitrary bucket or replace policies from the browser.
    assert (
        await console.http.post(
            "/v1/security-review/apply",
            json={"revision": 2, "bucket": "other"},
            headers=console.change_headers(),
        )
    ).status_code == 422
    review.config = CONFIG.model_copy(update={"allowed_user_ids": ["someone-else"]})
    assert (await console.http.get("/v1/security-review")).status_code == 404
    assert (await console.http.get("/v1/applications")).status_code == 200
    await console.http.aclose()


async def test_unconfigured_feature_does_not_break_platform(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTROL_PLANE_SECURITY_REVIEW_CONFIG", raising=False)
    console = Console(world, SETTINGS)
    await console.login()
    assert (await console.http.get("/v1/security-review")).status_code == 503
    assert (await console.http.get("/v1/applications")).status_code == 200
    assert (await console.http.get("/v1/workflows")).status_code == 200
    await console.http.aclose()


def test_cloud_provider_uses_versioned_reads_and_only_writes_extra_bucket(monkeypatch) -> None:
    import httpx

    from control_plane.security_review import provider as module
    from control_plane.security_review.provider import CloudProvider

    calls = []
    cfg = CONFIG.model_copy(update={"mode": "cloud"})
    cloud = CloudProvider(cfg, "ap-northeast-2")
    monkeypatch.setattr(cloud, "_headers", lambda: {"Authorization": "test-token"})

    class Iam:
        def get_role_policy(self, **kwargs):
            assert kwargs == {"RoleName": cfg.aws_role, "PolicyName": cfg.aws_policy}
            return {"PolicyDocument": FixtureProvider(cfg).data["source"]}

    monkeypatch.setattr(module.boto3, "client", lambda *a, **kw: Iam())

    def get(url, **kwargs):
        calls.append(("get", url))
        assert kwargs["params"] == {"optionsRequestedPolicyVersion": 3}
        key = "extra" if cfg.extra_bucket in url else "target"
        return httpx.Response(
            200, json=FixtureProvider(cfg).data[key], request=httpx.Request("GET", url)
        )

    def put(url, **kwargs):
        calls.append(("put", url))
        policy = kwargs["json"]
        assert policy["etag"] == "extra-1" and policy["version"] == 3
        assert policy["bindings"][0]["members"] == [
            "serviceAccount:other@demo-project.iam.gserviceaccount.com"
        ]
        assert url.endswith(f"/{cfg.extra_bucket}/iam")
        return httpx.Response(200, json=policy, request=httpx.Request("PUT", url))

    monkeypatch.setattr(module.httpx, "get", get)
    monkeypatch.setattr(module.httpx, "put", put)
    snapshot = cloud.collect()
    cloud.apply(without_extra_grant(snapshot["extra"], cfg.gcp_principal))
    assert [method for method, _ in calls] == ["get", "get", "put"]


async def test_ecs_inline_config_is_allowlisted_and_fixture_is_local_only(
    world, monkeypatch
) -> None:
    import json

    console = Console(world, SETTINGS.model_copy(update={"environment": "production"}))
    await console.login()
    user_id = (await console.http.get("/v1/me")).json()["user"]["id"]
    cfg = CONFIG.model_copy(update={"allowed_user_ids": [user_id]})
    monkeypatch.setenv(
        "CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON", cfg.model_dump_json(by_alias=True)
    )
    # Production cannot activate fixtures even with an authenticated allowlisted user.
    assert (await console.http.get("/v1/security-review")).status_code == 503
    data = json.loads(cfg.model_dump_json(by_alias=True))
    data["mode"] = "cloud"
    monkeypatch.setenv("CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON", json.dumps(data))
    response = await console.http.get("/v1/security-review")
    assert response.status_code == 200 and response.json()["phase"] == "idle"
    assert response.headers["Cache-Control"] == "no-store"
    await console.http.aclose()


@pytest.mark.parametrize("unsafe", ["nonempty", "unlabelled", "public"])
def test_demo_setup_refuses_unsafe_existing_buckets(tmp_path, monkeypatch, unsafe) -> None:
    import sys

    import httpx

    from control_plane.security_review import prepare

    cfg = CONFIG.model_copy(update={"extra_bucket": "foxtrot-security-demo-test", "mode": "cloud"})
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json(by_alias=True))
    monkeypatch.setattr(sys, "argv", ["prepare", "--config", str(path), "--project", "app-project"])

    class Cloud:
        def __init__(self, *args):
            pass

        def _headers(self):
            return {}

        def _policy(self, *args):
            pytest.fail("unsafe bucket must not reach policy operations")

    monkeypatch.setattr(prepare, "CloudProvider", Cloud)
    original_client = httpx.Client

    def handle(request):
        assert request.method == "GET"
        metadata = {
            "labels": {"purpose": "foxtrot-security-demo"},
            "iamConfiguration": {"publicAccessPrevention": "enforced"},
        }
        if request.url.path.endswith("/o"):
            return httpx.Response(
                200, json={"items": [{"name": "existing-object"}]} if unsafe == "nonempty" else {}
            )
        if unsafe == "unlabelled":
            metadata["labels"] = {}
        if unsafe == "public":
            metadata["iamConfiguration"]["publicAccessPrevention"] = "inherited"
        return httpx.Response(200, json=metadata)

    monkeypatch.setattr(
        prepare.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ValueError):
        prepare.main()
