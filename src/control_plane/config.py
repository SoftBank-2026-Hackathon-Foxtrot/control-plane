"""Process settings, from the environment. What the platform manages is in the seed file."""

from __future__ import annotations

import os
from typing import Literal, Self
from urllib.parse import urlsplit

import boto3
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTROL_PLANE_", env_file=".env", extra="ignore")

    environment: Literal["production", "local"] = "production"
    host: str = "0.0.0.0"
    port: int = 8000
    seed_path: str = "config/seed.yaml"
    onprem_planning: bool = False
    onprem_actors: dict[str, int] = Field(default_factory=dict)

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
    observation_token_secret: str | None = None
    prometheus_url: str | None = None
    tempo_url: str | None = None
    ai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "CONTROL_PLANE_AI_API_KEY")
    )
    ai_api_key_secret: str | None = None
    ai_model: str = "gpt-5-mini"
    ai_timeout_seconds: float = Field(default=60, ge=1, le=120)
    ai_max_output_tokens: int = Field(default=4000, ge=256, le=16000)
    ai_retention_days: int = Field(default=30, ge=1, le=90)
    ai_application_label: str = Field(default="application_id", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    ai_environment_label: str = Field(
        default="deployment_environment_name", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$"
    )
    public_probe_url: str | None = None
    public_probe_served_by: dict[str, str] = {}
    """X-Served-By values mapped to registered environment names; separate from login public_url."""

    public_url: str = "http://localhost:5173"
    allowed_origins: list[str] = ["http://localhost:5173"]
    github_client_id: str = ""
    github_client_secret_ref: str = "env:GITHUB_CLIENT_SECRET"
    github_allowed_ids: list[int] = []
    session_hours: int = 8
    auth_disabled: bool = False
    """Skips login for a local console. Refused outside `environment=local`."""

    @field_validator("public_probe_url", "prometheus_url", "tempo_url")
    @classmethod
    def valid_probe_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("public_probe_url must be HTTP(S) without embedded credentials")
        return value

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
