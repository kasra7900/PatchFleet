"""Small, versioned catalogue of user-selectable engineering requirements."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineeringProfile:
    profile_id: str
    version: str
    required_sections: tuple[str, ...]
    checks: tuple[str, ...]
    planning_questions: tuple[str, ...]

    def requirement_ids(self) -> tuple[str, ...]:
        return tuple(
            f"profile:{self.profile_id}@{self.version}:{section}"
            for section in self.required_sections
        )


CATALOGUE: dict[str, EngineeringProfile] = {
    "python-cli": EngineeringProfile(
        "python-cli",
        "0.1",
        ("test_strategy", "considerations.documentation", "considerations.api_contracts"),
        ("document_public_cli", "test_cli_behavior"),
        ("Which public commands or options change?", "How will command failures be tested?"),
    ),
    "python-service": EngineeringProfile(
        "python-service",
        "0.1",
        (
            "test_strategy",
            "considerations.reliability",
            "considerations.observability",
            "considerations.api_contracts",
        ),
        ("service_failure_modes", "document_api_contracts"),
        ("What are the failure modes?", "How will operators observe failures?"),
    ),
    "security-sensitive": EngineeringProfile(
        "security-sensitive",
        "0.1",
        ("considerations.security", "considerations.rollback", "risks"),
        ("threats_and_mitigations", "risk_attention"),
        ("What trust boundaries change?", "Which risks require user acceptance?"),
    ),
    "database-change": EngineeringProfile(
        "database-change",
        "0.1",
        (
            "considerations.migration",
            "considerations.rollback",
            "considerations.reliability",
            "test_strategy",
        ),
        ("migration_sequence", "rollback_for_schema_change"),
        ("How will data be migrated?", "How can the schema change be rolled back?"),
    ),
}


def selected_profiles(ids: tuple[str, ...]) -> tuple[EngineeringProfile, ...]:
    """Resolve only explicit charter IDs; never infer a profile."""
    unknown = sorted(set(ids) - CATALOGUE.keys())
    if unknown:
        raise ValueError(f"unknown engineering profile(s): {', '.join(unknown)}")
    return tuple(CATALOGUE[profile_id] for profile_id in ids)
