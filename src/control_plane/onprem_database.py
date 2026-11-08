"""Build a DB adapter request for an AWS → on-prem plan without touching the cloud seed."""

import re
from typing import Any

from control_plane.domain.models import EndpointPair, ResourceKind
from control_plane.errors import ApiError

ONPREM_DB_PROFILE = "postgres-rds-onprem-logical-pgbouncer-v1"
_SECRET = re.compile(
    r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+"
)
_RDS = re.compile(r"arn:aws[a-z-]*:rds:[a-z0-9-]+:[0-9]{12}:db:[A-Za-z0-9-]+")
_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def migration_request(workflow_id: str, pair: EndpointPair) -> dict[str, Any]:
    source, target = pair.source, pair.target
    if (
        source.provider != "AWS_RDS"
        or target.provider != "SELF_MANAGED"
        or not _RDS.fullmatch(source.provider_instance or "")
        or target.provider_instance is not None
        or source.writer_role != target.writer_role
        or (
            source.host.lower() == target.host.lower()
            and (source.port or 5432) == (target.port or 5432)
        )
    ):
        raise ApiError(
            422,
            "INVALID_ONPREM_DATABASE",
            "use distinct RDS and self-managed endpoints with the same writer",
        )
    for db in (source, target):
        if (
            not _SECRET.fullmatch(db.secret_ref)
            or not _IDENT.fullmatch(db.writer_role or "")
            or not _IDENT.fullmatch(db.database)
            or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", db.host)
            or db.options is not None
        ):
            raise ApiError(
                422, "INVALID_ONPREM_DATABASE", "use a database host, role and Secrets Manager ARN"
            )
    return {
        "workflowId": workflow_id,
        "resourceId": "database",
        "kind": "DATABASE",
        "profile": ONPREM_DB_PROFILE,
        "source": source.wire(ResourceKind.DATABASE),
        "target": target.wire(ResourceKind.DATABASE),
        "policy": {"rollbackMode": "BEFORE_TARGET_WRITE", "maxQuiesceSeconds": 60},
    }
