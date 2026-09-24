"""Deterministic readiness gate for an evidence-backed planning dossier."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .charter import (
    CharterValidationError,
    charter_fingerprint,
    charter_rule_ids,
    load_charter,
)
from .context import ContextError, inspect_context, load_context
from .knowledge_contracts import knowledge_evidence_id
from .knowledge_store import KnowledgeStore, KnowledgeStoreError
from .leader_contracts import EvidenceReference, PlanningDossier
from .profiles import selected_profiles
from .validation import PlanValidationError, validate_plan
from .worktrees import WorktreeError, inspect_repository

VALIDATED_CAPABILITIES = frozenset(
    {
        "context_inspection",
        "charter_validation",
        "plan_validation",
        "architecture_gate",
        "prompt_compilation",
        "local_knowledge_retrieval",
    }
)
PROFILE_CHECK_SECTIONS = {
    "document_public_cli": "quality_commitments.documented_public_cli",
    "test_cli_behavior": "quality_commitments.tests",
    "service_failure_modes": "considerations.reliability",
    "document_api_contracts": "considerations.api_contracts",
    "threats_and_mitigations": "considerations.security",
    "risk_attention": "risks",
    "migration_sequence": "considerations.migration",
    "rollback_for_schema_change": "considerations.rollback",
}
UNSELECTED = frozenset(
    {"auto", "default", "tbd", "unknown", "placeholder", "user_selection_required"}
)


@dataclass(frozen=True)
class ArchitectureIssue:
    path: str
    code: str
    message: str


class ArchitectureGateError(ValueError):
    def __init__(self, issues: list[ArchitectureIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


def _location(parts: tuple[str | int, ...]) -> str:
    value = ""
    for part in parts:
        value += f"[{part}]" if isinstance(part, int) else ("." if value else "") + part
    return value or "dossier"


def _present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _section(dossier: PlanningDossier, path: str) -> object:
    value: object = dossier
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _selection_missing(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        not normalized
        or normalized in UNSELECTED
        or (normalized.startswith("<") and normalized.endswith(">"))
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
    )


def _knowledge_citation_ids(root: Path) -> set[str]:
    """Chunk citation IDs available from the local Phase 3B knowledge store."""
    try:
        with KnowledgeStore(root, read_only=True) as store:
            return {knowledge_evidence_id(chunk.chunk_id) for chunk in store.all_chunks()}
    except (KnowledgeStoreError, sqlite3.Error, OSError):
        return set()


def validate_dossier(value: object, repository: Path) -> PlanningDossier:
    """Return a ready dossier or structured issues; never create a run or approval."""
    try:
        dossier = PlanningDossier.model_validate(value)
    except ValidationError as error:
        raise ArchitectureGateError(
            [
                ArchitectureIssue(_location(tuple(item["loc"])), item["type"], item["msg"])
                for item in error.errors()
            ]
        ) from error

    issues: list[ArchitectureIssue] = []

    def issue(path: str, code: str, message: str) -> None:
        issues.append(ArchitectureIssue(path, code, message))

    try:
        root, head = inspect_repository(repository)
    except (OSError, WorktreeError) as error:
        raise ArchitectureGateError(
            [ArchitectureIssue("repository", "unavailable", str(error))]
        ) from error
    try:
        charter = load_charter(root)
    except CharterValidationError as error:
        raise ArchitectureGateError(
            [
                ArchitectureIssue(f"charter.{item.path}", item.code, item.message)
                for item in error.issues
            ]
        ) from error
    try:
        context = load_context(root, dossier.context_id)
        current = inspect_context(root, context.selected_paths)
    except (ContextError, OSError, WorktreeError) as error:
        raise ArchitectureGateError(
            [ArchitectureIssue("context_id", "unavailable", str(error))]
        ) from error
    if context.context_id != current.context_id:
        issue("context_id", "stale_context", "repository evidence changed; inspect context again")
    if dossier.base_commit != head or context.base_commit != head:
        issue("base_commit", "base_mismatch", "dossier and context must match the current Git HEAD")
    if dossier.charter_fingerprint != charter_fingerprint(charter):
        issue("charter_fingerprint", "charter_changed", "charter changed; revise the dossier")

    profiles = selected_profiles(charter.profiles)
    expected_versions = {profile.profile_id: profile.version for profile in profiles}
    if dossier.profile_versions != expected_versions:
        issue(
            "profile_versions",
            "profile_mismatch",
            f"use exactly the charter-selected profile versions: {expected_versions}",
        )
    for profile in profiles:
        for section in profile.required_sections:
            if not _present(_section(dossier, section)):
                issue(
                    section,
                    "profile_section_required",
                    f"required by {profile.profile_id}@{profile.version}",
                )
        for check in profile.checks:
            section = PROFILE_CHECK_SECTIONS[check]
            if not _present(_section(dossier, section)):
                issue(
                    section,
                    "profile_check_required",
                    f"{check} required by {profile.profile_id}@{profile.version}",
                )

    repo_ids = {item.evidence_id for item in context.evidence}
    charter_ids = set(charter_rule_ids(charter))
    knowledge_ids = _knowledge_citation_ids(root)
    profile_ids = {
        reference
        for profile in profiles
        for reference in (
            *profile.requirement_ids(),
            *(
                f"profile:{profile.profile_id}@{profile.version}:check:{check}"
                for check in profile.checks
            ),
        )
    }

    def check_references(
        refs: tuple[EvidenceReference, ...], path: str, *, repository_only: bool = False
    ) -> None:
        if not refs:
            issue(
                path,
                "evidence_required",
                "cite inspected repository, charter, profile, or local knowledge evidence",
            )
        for index, ref in enumerate(refs):
            valid = {
                "repository": repo_ids,
                "charter": charter_ids,
                "profile": profile_ids,
                "knowledge": knowledge_ids,
            }[ref.source]
            if repository_only and ref.source != "repository":
                issue(
                    f"{path}[{index}]",
                    "repository_evidence_required",
                    "finding needs a repository locator",
                )
            elif ref.reference_id not in valid:
                issue(
                    f"{path}[{index}]",
                    "unknown_evidence",
                    "reference is absent from inspected context, charter, or selected profiles",
                )

    for index, finding in enumerate(dossier.repository_findings):
        if not finding.statement.strip():
            issue(
                f"repository_findings[{index}].statement", "blank", "finding statement is required"
            )
        check_references(
            finding.evidence, f"repository_findings[{index}].evidence", repository_only=True
        )

    if dossier.architecture_style != charter.architecture.style:
        issue(
            "architecture_style",
            "style_mismatch",
            "use the architecture style selected in the charter",
        )
    required_rule_ids = {
        *(
            f"charter:architecture.forbidden_patterns[{index}]"
            for index, _ in enumerate(charter.architecture.forbidden_patterns)
        ),
        *(
            f"charter:non_negotiable_rules[{index}]"
            for index, _ in enumerate(charter.non_negotiable_rules)
        ),
    }
    for rule_id in sorted(required_rule_ids):
        if not _present(dossier.charter_compliance.get(rule_id)):
            issue(
                f"charter_compliance.{rule_id}",
                "rule_response_required",
                "explain how this charter rule is respected",
            )

    seen_decisions: set[str] = set()
    for index, decision in enumerate(dossier.architecture_decisions):
        path = f"architecture_decisions[{index}]"
        if decision.decision_id in seen_decisions:
            issue(f"{path}.decision_id", "duplicate_decision", "decision IDs must be unique")
        seen_decisions.add(decision.decision_id)
        for field in ("statement", "rationale"):
            if not _present(getattr(decision, field)):
                issue(f"{path}.{field}", "blank", f"decision {field} is required")
        for alt_index, alternative in enumerate(decision.alternatives):
            if not _present(alternative.option) or not _present(alternative.trade_off):
                issue(
                    f"{path}.alternatives[{alt_index}]",
                    "incomplete_alternative",
                    "name the alternative and its trade-off",
                )
        check_references(decision.evidence, f"{path}.evidence")

    for index, change in enumerate(dossier.contract_changes):
        if not _present(change.description) or not _present(change.compatibility):
            issue(
                f"contract_changes[{index}]",
                "incomplete_contract",
                "describe the change and compatibility",
            )
    for index, risk in enumerate(dossier.risks):
        if not _present(risk.description) or not _present(risk.mitigation):
            issue(f"risks[{index}]", "incomplete_risk", "describe the risk and mitigation")
        if risk.severity in {"high", "critical"} and not risk.requires_user_attention:
            issue(
                f"risks[{index}].requires_user_attention",
                "risk_attention_required",
                "high risk needs explicit user attention",
            )
        check_references(risk.evidence, f"risks[{index}].evidence")
    if "security-sensitive" in charter.profiles and not any(
        risk.requires_user_attention for risk in dossier.risks
    ):
        issue(
            "risks",
            "risk_attention_required",
            "security-sensitive profile needs a risk marked for user attention",
        )

    high_risk = dossier.change_risk in {"high", "critical"} or any(
        risk.severity in {"high", "critical"} for risk in dossier.risks
    )
    data_change = any(change.kind == "data" for change in dossier.contract_changes)
    required_considerations = {
        "security": charter.security_sensitivity in {"high", "critical"}
        or charter.risk_policy.require_security_review,
        "migration": charter.risk_policy.require_migration_plan and (high_risk or data_change),
        "rollback": charter.risk_policy.require_rollback_for_schema_change and data_change,
        "observability": charter.risk_policy.require_observability_plan,
    }
    for name, required in required_considerations.items():
        if required and not _present(getattr(dossier.considerations, name)):
            issue(
                f"considerations.{name}",
                "charter_treatment_required",
                f"charter requires explicit {name} treatment",
            )
    if high_risk and not _present(dossier.considerations.security):
        issue(
            "considerations.security",
            "high_risk_treatment_required",
            "high risk requires a security/risk treatment",
        )
    if high_risk and not any(
        risk.severity in {"high", "critical"} and risk.requires_user_attention
        for risk in dossier.risks
    ):
        issue("risks", "high_risk_treatment_required", "record risks requiring user attention")

    quality = charter.quality
    for name, required in (
        ("tests", quality.require_tests),
        ("type_hints", quality.require_type_hints),
        ("documented_public_cli", quality.require_documented_public_cli),
        ("documentation", quality.require_documentation),
        ("api_contracts", quality.require_api_contracts),
    ):
        if required and not _present(getattr(dossier.quality_commitments, name)):
            issue(
                f"quality_commitments.{name}",
                "quality_commitment_required",
                f"charter requires {name} treatment",
            )
    for check in quality.required_checks:
        if check not in dossier.quality_commitments.planned_checks:
            issue(
                "quality_commitments.planned_checks",
                "required_check_missing",
                f"include charter check: {check}",
            )

    try:
        validate_plan(dossier.proposed_plan)
    except PlanValidationError as error:
        for item in error.issues:
            issue(f"proposed_plan.{item.path}", item.code, item.message)
    if dossier.leader.selected_role != "leader" or dossier.proposed_plan.leader != dossier.leader:
        issue(
            "leader",
            "leader_mismatch",
            "explicit Leader must match the proposed Plan's Leader assignment",
        )
    selections = [dossier.leader, dossier.proposed_plan.reviewer]
    selections.extend(dossier.proposed_plan.tasks)
    selections.extend(task.reviewer for task in dossier.proposed_plan.tasks)
    for index, selection in enumerate(selections):
        if _selection_missing(selection.assigned_provider) or _selection_missing(
            selection.selected_model
        ):
            issue(
                f"assignments[{index}]",
                "user_selection_required",
                "provider/model placeholders cannot authorize a ready dossier",
            )

    expected_dependencies = {
        task.task_id: tuple(sorted(task.dependencies)) for task in dossier.proposed_plan.tasks
    }
    recommended = {
        item.task_id: tuple(sorted(item.dependencies)) for item in dossier.recommended_dependencies
    }
    if (
        len(recommended) != len(dossier.recommended_dependencies)
        or recommended != expected_dependencies
    ):
        issue(
            "recommended_dependencies",
            "graph_mismatch",
            "recommended graph must exactly match the proposed Plan",
        )
    for index, claim in enumerate(dossier.capability_claims):
        if claim.status == "validated":
            check_references(claim.evidence, f"capability_claims[{index}].evidence")
        if claim.status == "validated" and claim.capability not in VALIDATED_CAPABILITIES:
            issue(
                f"capability_claims[{index}]",
                "unsupported_capability",
                "cannot claim this capability is validated in Phase 3A",
            )
    if dossier.unresolved_questions:
        issue(
            "unresolved_questions",
            "clarification_required",
            "resolve questions with the user before marking the dossier ready",
        )
    if issues:
        raise ArchitectureGateError(issues)
    return dossier
