"""Traffic weights, probes, the workload coordinator, seed validation and the adapter client."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from control_plane.adapters.client import AdapterClient, AdapterError, AdapterUnreachable
from control_plane.adapters.profiles import PROFILES
from control_plane.domain.models import (
    ActorState,
    HoldDesired,
    ResourceKind,
    ResourceState,
    Workflow,
)
from control_plane.probes import HttpAppProber, p95
from control_plane.seed import Seed, load_seed
from control_plane.traffic import CloudflareTraffic, TrafficError, ratio_weights
from tests.conftest import SEED, World
from tests.fakes import FakeClock


# ---- traffic ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("percent", "source", "target"),
    [
        (0, 1.0, 0.0),
        (100, 0.0, 1.0),
        (50, 1.0, 1.0),
        (25, 0.9, 0.3),
        (75, 0.3, 0.9),
    ],
)
def test_weights_in_tenths_give_the_requested_split(
    percent: int, source: float, target: float
) -> None:
    assert ratio_weights(percent, step=0.1) == (source, target)


def test_five_percent_cannot_be_expressed_in_tenths() -> None:
    source, target = ratio_weights(5, step=0.1)
    assert (source, target) == (1.0, 0.1)
    assert round(target / (source + target) * 100, 1) == 9.1


def test_a_finer_step_reaches_five_percent() -> None:
    source, target = ratio_weights(5, step=0.01)
    assert round(target / (source + target) * 100, 1) == 5.0


class Cloudflare:
    def __init__(self) -> None:
        self.steering: dict[str, Any] = {}
        self.fail_with: int | None = None
        self.lie = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/client/v4/zones/zone/load_balancers/lb"
        assert request.headers["authorization"] == "Bearer cf-token"
        if self.fail_with:
            return httpx.Response(
                self.fail_with, json={"success": False, "errors": [{"message": "no"}]}
            )
        if request.method == "PATCH":
            body = json.loads(request.content)
            self.steering = body["random_steering"]
            assert body["steering_policy"] == "random"
        steering = {"pool_weights": {}} if self.lie else self.steering
        return httpx.Response(200, json={"success": True, "result": {"random_steering": steering}})

    def client(self) -> CloudflareTraffic:
        return CloudflareTraffic(
            httpx.AsyncClient(
                transport=httpx.MockTransport(self),
                base_url="https://api.cloudflare.test/client/v4/",
            ),
            token="cf-token",
            zone_id="zone",
            load_balancer_id="lb",
        )


async def test_weight_change_is_read_back_and_reports_the_real_split() -> None:
    cloudflare = Cloudflare()
    applied = await cloudflare.client().set_split(
        source_pool="pool-aws", target_pool="pool-gcp", target_percent=5
    )
    assert [r.method for r in cloudflare.requests] == ["PATCH", "GET"]
    assert cloudflare.steering["pool_weights"] == {"pool-aws": 1.0, "pool-gcp": 0.1}
    assert cloudflare.steering["default_weight"] == 0
    assert applied.effective_percent == {"pool-aws": 90.9, "pool-gcp": 9.1}


async def test_a_weight_change_that_did_not_stick_is_an_error() -> None:
    cloudflare = Cloudflare()
    cloudflare.lie = True
    with pytest.raises(TrafficError, match="read back"):
        await cloudflare.client().set_split(
            source_pool="pool-aws", target_pool="pool-gcp", target_percent=50
        )


async def test_cloudflare_refusal_is_an_error() -> None:
    cloudflare = Cloudflare()
    cloudflare.fail_with = 403
    with pytest.raises(TrafficError, match="403"):
        await cloudflare.client().set_split(
            source_pool="pool-aws", target_pool="pool-gcp", target_percent=50
        )


# ---- adapter client ------------------------------------------------------------------------------
def client_answering(handler: Any) -> AdapterClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://a")
    return AdapterClient(http, "token")


async def test_errors_keep_the_adapter_code_and_blockers() -> None:
    client = client_answering(
        lambda _: httpx.Response(
            409, json={"code": "REVISION_CONFLICT", "message": "m", "blockers": ["b"]}
        )
    )
    with pytest.raises(AdapterError) as error:
        await client.migration("m")
    assert (error.value.code, error.value.blockers) == ("REVISION_CONFLICT", ["b"])


async def test_a_body_in_another_shape_still_becomes_an_error() -> None:
    client = client_answering(lambda _: httpx.Response(401, json={"detail": "bad token"}))
    with pytest.raises(AdapterError) as error:
        await client.migration("m")
    assert (error.value.code, error.value.message) == ("HTTP_401", "bad token")


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_5xx_is_not_a_refusal(status: int) -> None:
    client = client_answering(lambda _: httpx.Response(status))
    with pytest.raises(AdapterUnreachable):
        await client.act("m", "key", {"action": "CUTOVER"})


# ---- coordinator ---------------------------------------------------------------------------------
async def test_an_actor_counts_as_held_only_when_every_process_is(world: World) -> None:
    coordinator = world.services.coordinator
    hold = await coordinator.request_hold("wf")
    assert hold.generation == 1

    world.workload.generation = 1
    world.workload.desired = HoldDesired.HOLD
    world.workload.processes = world.workload.processes[:-1]  # one worker has not reported
    await world.workload.beat()
    reached, waiting = await coordinator.all_reached(1, ActorState.HELD)
    assert not reached
    assert waiting == ["goods-gcp-worker (1/2 HELD, 0 in flight)"]


async def test_a_mix_of_states_is_not_held(world: World) -> None:
    coordinator = world.services.coordinator
    await coordinator.request_hold("wf")
    for actor, count in world.seed.all_actors().items():
        for index in range(count):
            await coordinator.heartbeat(
                actor_id=actor,
                instance_id=str(index),
                generation=1,
                state=ActorState.HOLDING if index == 0 else ActorState.HELD,
                in_flight=0,
            )
    reached, _ = await coordinator.all_reached(1, ActorState.HELD)
    assert not reached


async def test_requests_in_flight_block_held(world: World) -> None:
    coordinator = world.services.coordinator
    await coordinator.request_hold("wf")
    for actor, count in world.seed.all_actors().items():
        for index in range(count):
            await coordinator.heartbeat(
                actor_id=actor,
                instance_id=str(index),
                generation=1,
                state=ActorState.HELD,
                in_flight=1 if actor == "goods-aws-api" and index == 0 else 0,
            )
    reached, waiting = await coordinator.all_reached(1, ActorState.HELD)
    assert not reached and "1 in flight" in waiting[0]


async def test_a_report_from_an_older_generation_does_not_count(world: World) -> None:
    coordinator = world.services.coordinator
    await world.workload.beat()  # generation 0, RUNNING
    await coordinator.request_hold("wf")
    reached, _ = await coordinator.all_reached(1, ActorState.RUNNING)
    assert not reached


async def test_a_process_that_went_quiet_stops_counting(world: World) -> None:
    coordinator = world.services.coordinator
    await coordinator.request_hold("wf")
    world.workload.generation = 1
    world.workload.desired = HoldDesired.HOLD
    await world.workload.beat()
    assert (await coordinator.all_reached(1, ActorState.HELD))[0]
    world.clock.advance(world.seed.timings.instance_ttl + 1)
    assert not (await coordinator.all_reached(1, ActorState.HELD))[0]


async def test_a_restarted_process_learns_of_the_hold_on_its_first_call(world: World) -> None:
    coordinator = world.services.coordinator
    await coordinator.request_hold("wf")
    directive = await coordinator.heartbeat(
        actor_id="goods-aws-api",
        instance_id="just-started",
        generation=0,
        state=ActorState.HOLDING,
        in_flight=0,
    )
    assert directive.desired is HoldDesired.HOLD
    assert directive.generation == 1
    assert directive.poll_after_ms == world.seed.timings.fast_poll_ms


async def test_the_engine_ticks_fast_only_while_the_application_stands_still(
    world: World,
) -> None:
    engine = world.platform.engine
    timings = world.seed.timings
    assert await engine.interval() == timings.tick_interval
    await world.services.coordinator.request_hold("wf")
    assert await engine.interval() == timings.held_tick_interval
    await world.services.coordinator.release()
    assert await engine.interval() == timings.tick_interval


async def test_a_repeated_hold_request_stays_in_one_generation(world: World) -> None:
    coordinator = world.services.coordinator
    first = await coordinator.request_hold("wf")
    second = await coordinator.request_hold("wf")
    assert first.generation == second.generation
    await coordinator.release()
    third = await coordinator.request_hold("wf")
    assert third.generation == first.generation + 1


async def test_evidence_lists_each_actor_with_its_oldest_report(world: World) -> None:
    coordinator = world.services.coordinator
    await coordinator.request_hold("wf")
    world.workload.generation = 1
    world.workload.desired = HoldDesired.HOLD
    await world.workload.beat()
    participants = await coordinator.participants(1)
    assert {p["id"] for p in participants} == set(world.seed.all_actors())
    assert all(
        set(p) == {"id", "generation", "state", "inFlight", "observedAt"} for p in participants
    )


# ---- seed ----------------------------------------------------------------------------------------
def test_the_example_seed_is_valid() -> None:
    seed = load_seed(Path(__file__).parent.parent / "config" / "seed.example.yaml")
    assert seed.resources.object.expected_apps == sum(seed.all_actors().values())


def broken(change: Any) -> dict[str, Any]:
    raw = copy.deepcopy(SEED)
    change(raw)
    return raw


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s["resources"]["object"].pop("expectedApps"), "expectedApps"),
        (lambda s: s["resources"]["redis"].pop("bindingRef"), "bindingRef"),
        (lambda s: s["resources"]["database"].pop("catalog"), "catalog"),
        (lambda s: s.update(trafficWeights=[25, 50]), "end at 100"),
        (lambda s: s.update(canaryWeights=[150]), "percentages"),
        (
            lambda s: s["environments"]["gcp"]["actors"].update({"goods-aws-api": 1}),
            "unique",
        ),
        (lambda s: s.update(unknownField=1), "unknownField"),
    ],
)
def test_a_bad_seed_stops_the_process(change: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Seed.model_validate(broken(change))


# ---- probes --------------------------------------------------------------------------------------
class App:
    """The real application's endpoints, as far as the probe uses them."""

    def __init__(self) -> None:
        self.statuses = ["QUEUED", "CONFIRMED"]
        self.fail: dict[str, int] = {}
        self.calls: list[str] = []
        self.cleanup_body: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        for name, status in self.fail.items():
            if name in path:
                return httpx.Response(status)
        if path == "/readyz":
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/login/"):
            return httpx.Response(200, json={"sid": "sid"})
        assert request.headers["x-session"] == "sid"
        if path.endswith("/join"):
            assert request.headers["idempotency-key"]
            return httpx.Response(200, json={"request_id": "r1"})
        if path == "/api/me/requests":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            receipt = status == "CONFIRMED"
            return httpx.Response(200, json=[{"id": "r1", "status": status, "receipt": receipt}])
        if path == "/internal/probe/cleanup":
            self.cleanup_body = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})


def prober(app: App, *, cleanup: str | None = "/internal/probe/cleanup") -> HttpAppProber:
    raw = copy.deepcopy(SEED)
    raw["probe"]["cleanupPath"] = cleanup
    http = httpx.AsyncClient(transport=httpx.MockTransport(app))
    return HttpAppProber(http, Seed.model_validate(raw), FakeClock())


async def test_the_probe_walks_the_whole_order_path_and_cleans_up() -> None:
    app = App()
    probe = await prober(app).business("aws", "http://app")
    assert probe.passed, probe.reason
    assert probe.cleaned
    assert app.calls == [
        "/api/login/probe-platform",
        "/api/groupbuys/1/join",
        "/api/me/requests",
        "/api/me/requests",
        "/api/me/receipts/r1",
        "/internal/probe/cleanup",
        "/api/logout",
    ]
    assert app.cleanup_body == {"requestId": "r1"}
    assert probe.probe_id.startswith("evidence:aws:")


@pytest.mark.parametrize("step", ["join", "login", "receipts", "requests"])
async def test_a_failing_step_fails_the_probe(step: str) -> None:
    app = App()
    app.fail[step] = 500
    probe = await prober(app).business("gcp", "http://app")
    assert not probe.passed
    assert "500" in probe.reason


async def test_a_sold_out_order_fails_the_probe() -> None:
    app = App()
    app.statuses = ["SOLD_OUT"]
    probe = await prober(app).business("aws", "http://app")
    assert not probe.passed
    assert "SOLD_OUT" in probe.reason


async def test_without_a_cleanup_path_the_probe_says_it_left_data() -> None:
    probe = await prober(App(), cleanup=None).business("aws", "http://app")
    assert probe.passed
    assert not probe.cleaned


async def test_light_request_uses_a_session_and_reports_failures() -> None:
    app = App()
    subject = prober(app)
    passed, latency = await subject.light("http://app")
    assert passed and latency >= 0
    app.fail["groupbuys"] = 503
    passed, _ = await subject.light("http://app")
    assert not passed


async def test_readiness_is_a_plain_status_check() -> None:
    app = App()
    subject = prober(app)
    assert await subject.is_ready("http://app")
    app.fail["readyz"] = 503
    assert not await subject.is_ready("http://app")


def test_p95() -> None:
    assert p95([]) == 0
    assert p95(list(range(1, 101))) == 96


# ---- plan bodies ---------------------------------------------------------------------------------
def test_only_redis_gets_a_fractional_quiesce_budget() -> None:
    seed = Seed.model_validate(copy.deepcopy(SEED))
    workflow = Workflow(
        id="wf",
        application="goods",
        source_env="aws",
        target_env="gcp",
        created_by="t",
        created_at=FakeClock().now(),
    )
    bodies = {
        kind: PROFILES[kind].plan_body(
            workflow,
            seed.resources.of(kind),
            ResourceState(kind=kind, binding_ref="b", workload_scope_ref="s"),
            seed.scope,
        )
        for kind in PROFILES
    }
    assert bodies[ResourceKind.REDIS]["policy"]["maxQuiesceSeconds"] == 1.8
    for kind in (ResourceKind.DATABASE, ResourceKind.OBJECT_STORAGE):
        assert type(bodies[kind]["policy"]["maxQuiesceSeconds"]) is int, kind
