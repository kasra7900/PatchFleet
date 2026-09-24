"""User-owned, version-controlled engineering charter at the repository root."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from .contracts import StrictModel
from .profiles import selected_profiles

CHARTER_NAME = "patchfleet.project.yaml"
MAX_CHARTER_BYTES = 65536


@dataclass(frozen=True)
class CharterIssue:
    path: str
    code: str
    message: str


class CharterValidationError(ValueError):
    def __init__(self, issues: list[CharterIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


class TechnologyStack(StrictModel):
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    package_manifests: tuple[str, ...] = ()


class ArchitectureRules(StrictModel):
    style: str
    forbidden_patterns: tuple[str, ...] = ()


class QualityRules(StrictModel):
    required_checks: tuple[str, ...] = ()
    require_tests: bool = Field(default=False, strict=True)
    require_type_hints: bool = Field(default=False, strict=True)
    require_documented_public_cli: bool = Field(default=False, strict=True)
    require_documentation: bool = Field(default=False, strict=True)
    require_api_contracts: bool = Field(default=False, strict=True)


class RiskPolicy(StrictModel):
    require_migration_plan: bool = Field(default=False, strict=True)
    require_rollback_for_schema_change: bool = Field(default=False, strict=True)
    require_observability_plan: bool = Field(default=False, strict=True)
    require_security_review: bool = Field(default=False, strict=True)


class EngineeringCharter(StrictModel):
    schema_version: Literal["0.1"]
    technology_stack: TechnologyStack = TechnologyStack()
    profiles: tuple[str, ...]
    architecture: ArchitectureRules
    quality: QualityRules
    risk_policy: RiskPolicy = RiskPolicy()
    security_sensitivity: Literal["normal", "high", "critical"] = "normal"
    non_negotiable_rules: tuple[str, ...] = ()

    @field_validator("profiles")
    @classmethod
    def distinct_profiles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("profiles must not be duplicated")
        return values

    @field_validator("non_negotiable_rules")
    @classmethod
    def nonblank_rules(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("non-negotiable rules must not be blank")
        return values


def validate_charter(value: object) -> EngineeringCharter:
    try:
        charter = EngineeringCharter.model_validate(value)
    except ValidationError as error:
        raise CharterValidationError(
            [
                CharterIssue(
                    ".".join(map(str, item["loc"])) or "charter", item["type"], item["msg"]
                )
                for item in error.errors()
            ]
        ) from error
    issues: list[CharterIssue] = []
    try:
        selected_profiles(charter.profiles)
    except ValueError as error:
        issues.append(CharterIssue("profiles", "unknown_profile", str(error)))
    if not charter.architecture.style.strip():
        issues.append(CharterIssue("architecture.style", "blank", "architecture style is required"))
    for name, values in (
        ("technology_stack.languages", charter.technology_stack.languages),
        ("technology_stack.frameworks", charter.technology_stack.frameworks),
        ("quality.required_checks", charter.quality.required_checks),
    ):
        if any(not value.strip() for value in values):
            issues.append(CharterIssue(name, "blank", "entries must not be blank"))
    if issues:
        raise CharterValidationError(issues)
    return charter


def charter_fingerprint(charter: EngineeringCharter) -> str:
    data = charter.model_dump(mode="json")
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def charter_rule_ids(charter: EngineeringCharter) -> tuple[str, ...]:
    """Stable identifiers for concrete charter fields and list entries."""
    identifiers: list[str] = []

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key], f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        else:
            identifiers.append(f"charter:{path}")

    visit(charter.model_dump(mode="json"), "")
    return tuple(identifiers)


def load_charter(repository: Path) -> EngineeringCharter:
    path = repository / CHARTER_NAME
    if not path.is_file() or path.is_symlink():
        raise CharterValidationError(
            [
                CharterIssue(
                    CHARTER_NAME,
                    "missing",
                    "create a regular charter with 'patchfleet charter init'",
                )
            ]
        )
    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--", CHARTER_NAME],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if tracked.returncode:
        raise CharterValidationError(
            [CharterIssue(CHARTER_NAME, "untracked", "track the charter with Git before planning")]
        )
    if path.stat().st_size > MAX_CHARTER_BYTES:
        raise CharterValidationError(
            [CharterIssue(CHARTER_NAME, "oversized", "charter exceeds 64 KiB")]
        )
    try:
        return validate_charter(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise CharterValidationError(
            [CharterIssue(CHARTER_NAME, "unreadable", str(error))]
        ) from error


TEMPLATE = """# Edit this template, then track it in Git. No profile is selected for you.
schema_version: "0.1"
technology_stack:
  languages: []
  frameworks: []
  package_manifests: []
# Choose only from: python-cli, python-service, security-sensitive, database-change.
profiles: []
architecture:
  style: ""  # Required: name your architecture style.
  forbidden_patterns: []
quality:
  required_checks: []
  require_tests: false
  require_type_hints: false
  require_documented_public_cli: false
  require_documentation: false
  require_api_contracts: false
risk_policy:
  require_migration_plan: false
  require_rollback_for_schema_change: false
  require_observability_plan: false
  require_security_review: false
security_sensitivity: normal
non_negotiable_rules: []  # Add project-specific rules here.
"""


def init_charter(repository: Path) -> Path:
    """Explicitly create a template; never overwrite or stage it."""
    path = repository / CHARTER_NAME
    with path.open("x", encoding="utf-8") as target:
        target.write(TEMPLATE)
    return path
