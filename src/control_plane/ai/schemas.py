from __future__ import annotations

from datetime import timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from control_plane.domain.models import Model


class ChatRequest(Model):
    application_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, pattern=r"^conv_[a-f0-9]{32}$")
    environments: list[str] = Field(default_factory=list, max_length=10)
    deployment_id: str | None = Field(default=None, pattern=r"^env-[a-f0-9]{12}$")
    message: str = Field(min_length=1, max_length=4000)


class TimeRange(Model):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not timedelta(seconds=30) <= self.end - self.start <= timedelta(days=1):
            raise ValueError("analysis range must be between 30 seconds and 24 hours")
        return self


class AnalysisRequest(Model):
    application_id: str | None = Field(default=None, max_length=64)
    deployment_id: str = Field(pattern=r"^env-[a-f0-9]{12}$")
    environment: str | None = Field(default=None, min_length=1, max_length=64)
    time_range: TimeRange
    focus: list[Literal["RESOURCES", "PERFORMANCE", "ERRORS"]] = Field(
        default=["RESOURCES", "PERFORMANCE", "ERRORS"], min_length=1, max_length=3
    )


class RecommendationRequest(Model):
    application_id: str | None = Field(default=None, max_length=64)
    target_environment: str = Field(min_length=1, max_length=64)
    source_deployment_id: str = Field(pattern=r"^env-[a-f0-9]{12}$")


# Separate, simple model-output schemas keep the external JSON schema strict.
# Client inputs and returned values are checked again by the platform.
class ChatDraft(Model):
    answer: str
    source_ids: list[str]
    warnings: list[str]


class Finding(Model):
    category: Literal["RESOURCE", "PERFORMANCE", "ERROR", "DATA_QUALITY"]
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    description: str
    evidence_keys: list[str]


class AnalysisDraft(Model):
    summary: str
    findings: list[Finding]


class Parameter(Model):
    key: str
    value: str | int | float | bool
    rationale: str


class RecommendationDraft(Model):
    parameters: list[Parameter]
    app_parameters: list[Parameter]
    warnings: list[str]
