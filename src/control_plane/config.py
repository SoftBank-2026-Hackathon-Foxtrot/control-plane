"""Process settings, from the environment. What the platform manages is in the seed file."""

from __future__ import annotations

import os
from typing import Literal, Self

import boto3
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTROL_PLANE_", env_file=".env", extra="ignore")

    environment: Literal["production", "local"] = "production"
    host: str = "0.0.0.0"
    port: int = 8000
    seed_path: str = "config/seed.yaml"

    table_name: str = "foxtrot-control-plane"
    region: str = "ap-northeast-2"
    dynamodb_endpoint: str | None = None
    """Only for a local DynamoDB. Unset, or `memory` with `environment=local`, otherwise."""

    db_gate_table: str | None = None
    db_gate_namespace: str = "foxtrot-db-operator"

    traffic_url: str = "http://traffic.internal:8001"
    """The team's traffic service, the only writer of the Cloudflare load balancer."""
    traffic_token_secret: str = "env:TRAFFIC_SERVICE_TOKEN"

    workload_token_secret: str = "env:WORKLOAD_TOKEN"

    public_url: str = "http://localhost:5173"
    allowed_origins: list[str] = ["http://localhost:5173"]
    github_client_id: str = ""
    github_client_secret_ref: str = "env:GITHUB_CLIENT_SECRET"
    github_allowed_ids: list[int] = []
    session_hours: int = 8
    auth_disabled: bool = False
    personal_catalog_paths: dict[str, str] = {}
    allowed_secret_refs: list[str] = []
    seed_owner_id: str = ""
    """Explicit owner of the legacy seed. Blank keeps it hidden from signed-in users."""
    """Skips login for a local console. Refused outside `environment=local`."""

    @model_validator(mode="after")
    def safe(self) -> Self:
        if self.auth_disabled and self.environment != "local":
            raise ValueError("auth_disabled is allowed only with environment=local")
        if self.auth_disabled and self.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("auth_disabled would expose the control plane: bind to 127.0.0.1")
        if self.dynamodb_endpoint == "memory" and self.environment != "local":
            raise ValueError("the memory store is allowed only with environment=local")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.environment != "local"


def resolve_secret(reference: str, *, region: str | None = None) -> str:
    """`env:NAME` for values ECS injects, `aws:ID` to read Secrets Manager directly."""
    scheme, _, name = reference.partition(":")
    if scheme == "env":
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"the environment variable {name} is not set")
        return value
    if scheme == "aws":
        client = boto3.client("secretsmanager", region_name=region)
        return str(client.get_secret_value(SecretId=name)["SecretString"])
    raise ValueError(f"unknown secret reference {scheme!r}: use env:NAME or aws:ID")
