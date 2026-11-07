from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from control_plane.domain.models import Model

OBJECT_ROLES = frozenset(
    {"roles/storage.objectUser", "roles/storage.objectViewer", "roles/storage.objectAdmin"}
)


class ReviewConfig(Model):
    application: str = Field(min_length=1)
    allowed_user_ids: list[str] = Field(min_length=1)
    aws_role: str = Field(pattern=r"^[\w+=,.@-]{1,64}$")
    aws_policy: str = "workload-bucket"
    aws_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    gcp_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
    extra_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
    gcp_principal: str = Field(
        pattern=r"^serviceAccount:[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$"
    )
    gcp_credentials_ref: str | None = None
    mode: Literal["cloud", "fixture"] = "cloud"

    @model_validator(mode="after")
    def separate_buckets(self) -> Self:
        if self.gcp_bucket == self.extra_bucket:
            raise ValueError("the extra demo bucket must not be the application bucket")
        return self

    @property
    def key(self) -> str:
        return "SECURITY_REVIEW#" + fingerprint(self.model_dump())


class Change(Model):
    revision: int = Field(ge=0)
    plan_id: str | None = None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_scope_matches(document: dict[str, Any], bucket: str) -> bool:
    """Recognize the repository's workload-bucket policy, not arbitrary effective IAM."""
    allowed = {
        "s3:ListBucket": f"arn:aws:s3:::{bucket}",
        "s3:GetBucketLocation": f"arn:aws:s3:::{bucket}",
        "s3:GetObject": f"arn:aws:s3:::{bucket}/*",
        "s3:PutObject": f"arn:aws:s3:::{bucket}/*",
        "s3:DeleteObject": f"arn:aws:s3:::{bucket}/*",
    }
    found = set()
    statements = document.get("Statement", [])
    if not isinstance(statements, list) or not statements:
        return False
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            return False
        if set(statement) - {"Sid", "Effect", "Action", "Resource"}:
            return False
        actions = statement.get("Action", [])
        resources = statement.get("Resource", [])
        actions = [actions] if isinstance(actions, str) else actions
        resources = [resources] if isinstance(resources, str) else resources
        if not isinstance(actions, list) or not isinstance(resources, list):
            return False
        for action in actions:
            if not isinstance(action, str) or action not in allowed:
                return False
            if resources != [allowed[action]]:
                return False
            found.add(action)
    return set(allowed) <= found


def principal_bindings(policy: dict[str, Any], principal: str) -> list[dict[str, Any]]:
    return [binding for binding in policy.get("bindings", []) if principal in binding["members"]]


def without_extra_grant(policy: dict[str, Any], principal: str) -> dict[str, Any]:
    """Change only this principal's supported, unconditional object bindings; keep etag."""
    result = json.loads(json.dumps(policy))
    bindings = principal_bindings(result, principal)
    if (
        not result.get("etag")
        or not bindings
        or any(b.get("condition") or b["role"] not in OBJECT_ROLES for b in bindings)
    ):
        raise ValueError("unsupported extra bucket policy")
    for binding in bindings:
        binding["members"].remove(principal)
    result["bindings"] = [b for b in result["bindings"] if b["members"]]
    return dict(result)
