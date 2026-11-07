from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from control_plane.clock import Clock
from control_plane.errors import ApiError
from control_plane.store.table import Table, VersionConflict

from .models import (
    OBJECT_ROLES,
    Change,
    ReviewConfig,
    fingerprint,
    principal_bindings,
    source_scope_matches,
    without_extra_grant,
)
from .provider import Provider

BUSY = {"scanning", "applying", "verifying"}


class ReviewService:
    def __init__(
        self,
        config: ReviewConfig,
        provider: Provider,
        table: Table,
        clock: Clock,
        probe: Callable[[], Awaitable[bool]],
    ) -> None:
        self.config, self.provider, self.table, self.clock = config, provider, table, clock
        self.probe = probe

    async def load(self) -> dict[str, Any]:
        item = await self.table.get(self.config.key, "LATEST")
        if item:
            state = json.loads(item.body)
            state["revision"] = item.version
            return dict(state)
        return {
            "revision": 0,
            "phase": "idle",
            "checks": [],
            "events": [],
            "plan": None,
            "collectedAt": None,
            "verifiedAt": None,
            "appReady": None,
            "error": None,
            "snapshot": None,
        }

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        visible = {k: v for k, v in state.items() if k not in {"snapshot", "leaseUntil"}}
        visible.update(
            {
                "application": cfg.application,
                "mode": cfg.mode,
                "source": {
                    "role": cfg.aws_role,
                    "policy": cfg.aws_policy,
                    "bucket": cfg.aws_bucket,
                },
                "target": {
                    "principal": cfg.gcp_principal,
                    "bucket": cfg.gcp_bucket,
                    "extraBucket": cfg.extra_bucket,
                },
                "uiCanContinue": state["phase"] == "verified"
                and bool(state.get("appReady"))
                and bool(state.get("verifiedAt"))
                and self.clock.now().isoformat() < state.get("validUntil", ""),
            }
        )
        if state["phase"] in BUSY and self.clock.now().isoformat() >= state.get("leaseUntil", ""):
            visible.update(phase="error", error="작업 결과를 다시 확인해야 합니다.")
        return visible

    async def save(self, state: dict[str, Any]) -> None:
        try:
            state["revision"] = await self.table.put(
                self.config.key, "LATEST", json.dumps(state), expected_version=state["revision"]
            )
        except VersionConflict as error:
            raise ApiError(
                409, "REVIEW_CHANGED", "다른 작업이 진행되었습니다. 다시 조회하세요."
            ) from error

    def event(self, state: dict[str, Any], message: str, actor: str) -> None:
        state["events"] = [
            {
                "id": secrets.token_hex(8),
                "at": self.clock.now().isoformat(),
                "actor": actor,
                "message": message,
            },
            *state["events"],
        ][:50]

    def evaluate(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = self.config
        source_ok = source_scope_matches(snapshot["source"], cfg.aws_bucket)
        base = principal_bindings(snapshot["target"], cfg.gcp_principal)
        extra = principal_bindings(snapshot["extra"], cfg.gcp_principal)
        supported = all(not b.get("condition") and b["role"] in OBJECT_ROLES for b in extra)
        return [
            {
                "id": "source-scope",
                "status": "pass" if source_ok else "unknown",
                "title": "AWS 기준 정책",
                "source": cfg.aws_bucket,
                "target": "버킷 전용 정책 확인" if source_ok else "지원하지 않는 정책 형태",
                "evidence": f"IAM GetRolePolicy · {cfg.aws_role}/{cfg.aws_policy}",
            },
            {
                "id": "required-access",
                "status": "pass"
                if any(
                    b["role"] == "roles/storage.objectUser" and not b.get("condition") for b in base
                )
                else "unknown",
                "title": "앱에 필요한 접근 유지",
                "source": "객체 목록·읽기·쓰기·삭제",
                "target": cfg.gcp_bucket,
                "evidence": f"GCS getIamPolicy · {cfg.gcp_bucket} · roles/storage.objectUser",
            },
            {
                "id": "extra-access",
                "status": "unknown" if not supported else "fail" if extra else "pass",
                "title": "추가 버킷 접근",
                "source": "선택한 정책은 앱 버킷만 허용",
                "target": cfg.extra_bucket if extra else "추가 직접 권한 없음",
                "evidence": f"GCS getIamPolicy · {cfg.extra_bucket} · "
                + (", ".join(b["role"] for b in extra) or "해당 계정 바인딩 없음"),
            },
        ]

    async def collect(self, state: dict[str, Any]) -> None:
        snapshot = await asyncio.to_thread(self.provider.collect)
        state.update(
            snapshot=snapshot,
            checks=self.evaluate(snapshot),
            collectedAt=self.clock.now().isoformat(),
            verifiedAt=None,
            appReady=await self.probe(),
            plan=None,
        )

    @staticmethod
    def phase_after_scan(state: dict[str, Any]) -> str:
        if any(c["status"] == "unknown" for c in state["checks"]):
            return "unknown"
        return "gap" if any(c["status"] == "fail" for c in state["checks"]) else "matched"

    async def change(self, action: str, change: Change, actor: str) -> dict[str, Any]:
        state = await self.load()
        now = self.clock.now()
        if change.revision != state["revision"]:
            raise ApiError(409, "REVIEW_CHANGED", "검토 결과가 바뀌었습니다. 다시 조회하세요.")
        if state["phase"] in BUSY and now.isoformat() < state.get("leaseUntil", ""):
            raise ApiError(409, "REVIEW_BUSY", "작업이 진행 중입니다.")
        if action == "plan":
            if state["phase"] != "gap" or not state.get("appReady"):
                raise ApiError(409, "NOT_REVIEWABLE", "기준 정책과 앱 상태를 먼저 확인하세요.")
            policy = without_extra_grant(state["snapshot"]["extra"], self.config.gcp_principal)
            state.update(
                phase="plan",
                plan={
                    "id": secrets.token_hex(16),
                    "fingerprint": fingerprint(state["snapshot"]),
                    "expiresAt": (now + timedelta(minutes=10)).isoformat(),
                    "bucket": self.config.extra_bucket,
                    "principal": self.config.gcp_principal,
                    "removeRoles": [
                        b["role"]
                        for b in principal_bindings(
                            state["snapshot"]["extra"], self.config.gcp_principal
                        )
                    ],
                    "retainBucket": self.config.gcp_bucket,
                    "policyHash": fingerprint(policy),
                },
            )
            self.event(state, "추가 버킷 권한 변경안 생성", actor)
            await self.save(state)
            return self.view(state)
        plan = state.get("plan")
        if action == "apply" and (
            state["phase"] != "plan"
            or not plan
            or plan["id"] != change.plan_id
            or now.isoformat() >= plan["expiresAt"]
        ):
            raise ApiError(409, "PLAN_STALE", "변경안을 다시 생성하세요.")
        if action == "verify" and state["phase"] not in {"applied", "matched", "verified", "error"}:
            raise ApiError(409, "NOT_VERIFIABLE", "현재 설정을 먼저 조사하세요.")
        if action not in {"scan", "apply", "verify"}:
            raise ApiError(404, "NOT_FOUND", "unknown security action")
        state.update(
            phase={"scan": "scanning", "apply": "applying", "verify": "verifying"}[action],
            error=None,
            verifiedAt=None,
            appReady=None,
            leaseUntil=(now + timedelta(minutes=3)).isoformat(),
        )
        await self.save(state)
        try:
            if action == "apply":
                assert plan is not None
                latest = await asyncio.to_thread(self.provider.collect)
                if fingerprint(latest) != plan["fingerprint"]:
                    raise ApiError(
                        409, "PLAN_STALE", "클라우드 설정이 변경되었습니다. 다시 조사하세요."
                    )
                changed = without_extra_grant(latest["extra"], self.config.gcp_principal)
                await asyncio.to_thread(self.provider.apply, changed)
                state.update(phase="applied", plan=None)
                self.event(state, "GCP 변경 적용 · 재확인 필요", actor)
            else:
                await self.collect(state)
                state["phase"] = self.phase_after_scan(state)
                if action == "verify" and state["phase"] == "matched" and state["appReady"]:
                    state.update(
                        phase="verified",
                        verifiedAt=self.clock.now().isoformat(),
                        validUntil=(self.clock.now() + timedelta(minutes=10)).isoformat(),
                    )
                self.event(
                    state,
                    "설정 재확인 완료" if action == "verify" else "AWS·GCP 설정 조사 완료",
                    actor,
                )
        except Exception as error:
            # Never return cloud exceptions: they can contain credentials, URLs or raw policy data.
            message = (
                error.message
                if isinstance(error, ApiError)
                else "클라우드 조회·적용 결과를 확인하지 못했습니다. 다시 조사하세요."
            )
            state.update(phase="error", error=message, plan=None)
            self.event(state, "작업 실패 · 설정 재조회 필요", actor)
        await self.save(state)
        return self.view(state)
