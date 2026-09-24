"""Versioned, immutable data contracts for the local control plane."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    LEADER = "leader"
    WORKER = "worker"
    REVIEWER = "reviewer"


class RunState(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    VALIDATED = "VALIDATED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    WAITING_FOR_APPLY_APPROVAL = "WAITING_FOR_APPLY_APPROVAL"
    APPLIED = "APPLIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    CANCELLED = "CANCELLED"


class ApprovalType(StrEnum):
    PLAN_EXECUTION = "PLAN_EXECUTION"
    RESULT_APPLICATION = "RESULT_APPLICATION"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Assignment(StrictModel):
    assigned_provider: str
    selected_model: str
    selected_role: Role

    @field_validator("assigned_provider", "selected_model")
    @classmethod
    def require_selection(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("provider and model selections must not be blank")
        return value


class BudgetLimits(StrictModel):
    max_wall_time_seconds: int = Field(gt=0, strict=True)
    max_attempts: int = Field(gt=0, strict=True)
    max_output_bytes: int = Field(gt=0, strict=True)


class TaskSpec(StrictModel):
    task_id: Identifier
    title: str
    purpose: str
    dependencies: tuple[Identifier, ...]
    assigned_provider: str
    selected_model: str
    selected_role: Role
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    test_commands: tuple[str, ...] = Field(min_length=1)
    budget_limits: BudgetLimits
    reviewer: Assignment

    @field_validator("title", "purpose", "assigned_provider", "selected_model")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("allowed_paths", "acceptance_criteria", "test_commands")
    @classmethod
    def nonblank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        items = tuple(value.strip() for value in values)
        if any(not item for item in items):
            raise ValueError("list entries must not be blank")
        return items


class Plan(StrictModel):
    schema_version: Literal["0.1"]
    plan_id: Identifier
    title: str
    purpose: str
    leader: Assignment
    reviewer: Assignment
    tasks: tuple[TaskSpec, ...] = Field(min_length=1)

    @field_validator("title", "purpose")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class ApprovalRecord(StrictModel):
    approval_id: Identifier
    run_id: Identifier
    approval_type: ApprovalType
    plan_id: Identifier
    plan_revision: int = Field(gt=0)
    subject_fingerprint: Fingerprint
    timestamp: datetime
    actor: str
    decision: ApprovalDecision
    reason: str | None = None

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("actor")
    @classmethod
    def nonblank_actor(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class RunRecord(StrictModel):
    run_id: Identifier
    plan_id: Identifier
    plan_fingerprint: Fingerprint
    plan_revision: int = Field(gt=0)
    state: RunState
    reviewed_result_fingerprint: Fingerprint | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


def canonical_plan_json(plan: Plan) -> str:
    """Serialize a plan, normalizing order-insensitive collections."""
    data = plan.model_dump(mode="json")
    for task in data["tasks"]:
        task["dependencies"].sort()
        task["allowed_paths"].sort()
        task["acceptance_criteria"].sort()
    data["tasks"].sort(key=lambda task: task["task_id"])
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_fingerprint(plan: Plan) -> str:
    """Return the SHA-256 identity of the canonical logical plan."""
    return sha256(canonical_plan_json(plan).encode("utf-8")).hexdigest()
