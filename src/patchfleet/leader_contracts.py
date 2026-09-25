"""A planning dossier is evidence, not an executable run or approval."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator

from .contracts import Assignment, Fingerprint, Identifier, Plan, StrictModel


class EvidenceReference(StrictModel):
    source: Literal["repository", "charter", "profile", "knowledge"]
    reference_id: str

    @field_validator("reference_id")
    @classmethod
    def nonblank_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence reference ID must not be blank")
        return value


class RepositoryFinding(StrictModel):
    statement: str
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class DecisionAlternative(StrictModel):
    option: str
    trade_off: str


class ArchitectureDecision(StrictModel):
    decision_id: Identifier
    statement: str
    rationale: str
    alternatives: tuple[DecisionAlternative, ...] = Field(min_length=1)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    approval_basis: Literal["none"] = "none"


class ContractChange(StrictModel):
    kind: Literal["api", "data", "module"]
    description: str
    compatibility: str


class EngineeringConsiderations(StrictModel):
    security: str | None = None
    reliability: str | None = None
    performance: str | None = None
    migration: str | None = None
    rollback: str | None = None
    observability: str | None = None
    tests: str | None = None
    documentation: str | None = None
    api_contracts: str | None = None


class PlanningRisk(StrictModel):
    severity: Literal["low", "medium", "high", "critical"]
    description: str
    mitigation: str
    requires_user_attention: bool = Field(strict=True)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class TaskDependencyRecommendation(StrictModel):
    task_id: Identifier
    dependencies: tuple[Identifier, ...]


class CapabilityClaim(StrictModel):
    capability: str
    status: Literal["validated", "proposed", "unknown"]
    evidence: tuple[EvidenceReference, ...] = ()


class QualityCommitments(StrictModel):
    planned_checks: tuple[str, ...]
    tests: str | None = None
    type_hints: str | None = None
    documented_public_cli: str | None = None
    documentation: str | None = None
    api_contracts: str | None = None


class PlanningDossier(StrictModel):
    schema_version: Literal["0.1"]
    dossier_id: Identifier
    context_id: Fingerprint
    base_commit: str
    charter_fingerprint: Fingerprint
    profile_versions: dict[str, str]
    user_request: str
    non_goals: tuple[str, ...]
    change_risk: Literal["low", "medium", "high", "critical"]
    leader: Assignment
    assumptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    repository_findings: tuple[RepositoryFinding, ...] = Field(min_length=1)
    architecture_style: str
    architecture_decisions: tuple[ArchitectureDecision, ...] = Field(min_length=1)
    charter_compliance: dict[str, str]
    contract_changes: tuple[ContractChange, ...]
    considerations: EngineeringConsiderations
    quality_commitments: QualityCommitments
    test_strategy: str
    risks: tuple[PlanningRisk, ...]
    recommended_dependencies: tuple[TaskDependencyRecommendation, ...] = Field(min_length=1)
    capability_claims: tuple[CapabilityClaim, ...]
    proposed_plan: Plan

    @field_validator("user_request", "test_strategy")
    @classmethod
    def nonblank_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class LeaderTaskRationale(StrictModel):
    task_id: Identifier
    rationale: str

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task rationale must not be blank")
        return value


class LeaderQuestions(StrictModel):
    """The Leader asks focused clarification questions instead of planning."""

    schema_version: Literal["0.1"]
    outcome: Literal["questions"]
    message: str
    questions: tuple[str, ...] = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assistant message must not be blank")
        return value

    @field_validator("questions")
    @classmethod
    def nonblank_questions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("questions must not be blank")
        return cleaned


class LeaderPlanDraft(StrictModel):
    """A structured, unapproved plan draft returned by the Leader."""

    schema_version: Literal["0.1"]
    outcome: Literal["plan_draft"]
    message: str
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    task_rationale: tuple[LeaderTaskRationale, ...] = Field(min_length=1)
    worker_usage: str
    proposed_plan: Plan

    @field_validator("message", "worker_usage")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("assumptions", "risks")
    @classmethod
    def nonblank_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("entries must not be blank")
        return cleaned


LeaderResponse = Annotated[LeaderQuestions | LeaderPlanDraft, Field(discriminator="outcome")]

_RESPONSE_ADAPTER: TypeAdapter = TypeAdapter(LeaderResponse)


def leader_response_schema() -> dict:
    """Return a provider-facing JSON Schema for the strict response contract.

    A plain ``anyOf`` of the two literal-outcome models is used so any
    standards-compliant structured-output mechanism can consume it.
    """
    return TypeAdapter(LeaderQuestions | LeaderPlanDraft).json_schema()


def parse_leader_response(text: str) -> LeaderQuestions | LeaderPlanDraft:
    """Parse a JSON Leader response; callers convert errors to a bounded message."""
    return _RESPONSE_ADAPTER.validate_json(text)
