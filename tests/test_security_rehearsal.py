from __future__ import annotations

import os
from unittest.mock import Mock

import httpx
import pytest

from control_plane.errors import ApiError
from control_plane.security_review import rehearsal, rehearsal_api
from control_plane.security_review.gcp_probe import _observe
from control_plane.security_review.models import ProbeAccounts
from tests.conftest import SETTINGS, World
from tests.test_api import Console
from tests.test_security_review import CONFIG, run, setup

BEFORE = dict(receipt="allowed", otherBucket="allowed", copy="allowed")
AFTER = dict(receipt="allowed", otherBucket="denied", copy="allowed")


def test_real_weed_when_configured() -> None:
    binary = os.environ.get("CONTROL_PLANE_SECURITY_WEED_BINARY")
    if not binary:
        pytest.skip("SeaweedFS binary not configured")
    result = rehearsal.run_rehearsal(binary, None)
    assert result["passed"] and result["before"] == BEFORE and result["after"] == AFTER


def test_agent_retries_failed_candidate_with_observations(monkeypatch):
    answers = [dict(AFTER, receipt="denied"), AFTER]
    monkeypatch.setattr(rehearsal, "_trial", Mock(side_effect=[BEFORE, *answers]))
    candidate = Mock(side_effect=[["Read"], list(rehearsal.APP_OBJECT_ACTIONS)])
    monkeypatch.setattr(rehearsal, "_candidate", candidate)
    result = rehearsal.run_rehearsal("test", "model")
    assert result["passed"] and result["method"] == "bedrock"
    assert [t["observations"] for t in result["attempts"]] == answers


@pytest.mark.parametrize(
    ("code", "message", "result"),
    [
        (200, "", "allowed"),
        (403, "Permission storage.objects.list denied", "denied"),
        (403, "Service disabled", "unknown"),
        (404, "", "unknown"),
        (401, "", "unknown"),
    ],
)
def test_list_outcome_distinguishes_permission_from_other_errors(code, message, result):
    transport = httpx.MockTransport(
        lambda _: httpx.Response(code, json={"error": {"message": message}})
    )
    with httpx.Client(transport=transport) as client:
        assert _observe(client, "https://example.test/o", "token", listing=True) == result


async def test_probe_failure_blocks_plan_and_drift_blocks_apply(world: World, monkeypatch):
    review, provider = setup(world)
    accounts = ProbeAccounts(
        app_secret_ref="env:A", copy_secret_ref="env:B", copy_email="copy@g.iam.gserviceaccount.com"
    )
    review.config = CONFIG.model_copy(update={"probes": accounts})
    observations = Mock(side_effect=[dict(BEFORE, copy="unknown"), BEFORE, AFTER])
    monkeypatch.setattr(provider, "probe_access", observations)
    assert (await run(review, "scan"))["phase"] == "unknown"
    with pytest.raises(ApiError):
        await run(review, "plan")
    assert (await run(review, "scan"))["phase"] == "gap"
    await run(review, "plan")
    assert (await run(review, "apply"))["phase"] == "error"
    assert not provider.writes


async def test_active_workflow_change_invalidates_plan(world: World):
    review, provider = setup(world)
    await run(review, "scan")
    await run(review, "plan")
    await world.start()
    assert (await run(review, "apply"))["phase"] == "error"
    assert not provider.writes


async def test_rehearsal_approval_requires_current_test(world: World, monkeypatch):
    console = Console(world, SETTINGS)
    route = "/v1/security-review/onprem/rehearsal"
    await console.login()
    user = (await console.http.get("/v1/me")).json()["user"]["id"]
    monkeypatch.setenv("CONTROL_PLANE_SECURITY_REHEARSAL_USERS", user)
    monkeypatch.setattr(rehearsal_api, "binary_path", lambda: "local-test")
    monkeypatch.setattr(
        rehearsal_api,
        "run_rehearsal",
        lambda *args: {"passed": True, "afterActions": ["Read:receipts"]},
    )
    headers = console.change_headers()
    assert (await console.http.post(route + "/run", json={"revision": 0})).status_code == 403

    async def post(action, revision):
        return await console.http.post(
            route + "/" + action, json={"revision": revision}, headers=headers
        )

    tested = (await post("run", 0)).json()
    assert tested["status"] == "tested"
    assert (await post("approve", 0)).status_code == 409
    world.clock.advance(601)
    assert (await post("approve", tested["revision"])).status_code == 409
    tested = (await post("run", tested["revision"])).json()
    held = (await post("hold", tested["revision"])).json()
    assert held["status"] == "held" and (await post("approve", held["revision"])).status_code == 409
    tested = (await post("run", held["revision"])).json()
    decision = (await post("approve", tested["revision"])).json()
    assert decision["status"] == "approved" and decision["history"][0]["result"] == tested["result"]
    assert (await console.http.get(route)).json()["history"] == decision["history"]
    monkeypatch.setenv("CONTROL_PLANE_SECURITY_REHEARSAL_USERS", "other")
    assert (await console.http.get(route)).status_code == 404
    await console.http.aclose()
