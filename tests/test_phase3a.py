"""Leader planning is deterministic and never invokes agents or mutates Git history."""

from __future__ import annotations

import asyncio
import copy
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patchfleet.architecture_gate import ArchitectureGateError, validate_dossier
from patchfleet.charter import (
    CharterValidationError,
    charter_fingerprint,
    charter_rule_ids,
    load_charter,
    validate_charter,
)
from patchfleet.cli import app
from patchfleet.context import ContextError, inspect_context, load_context, persist_context
from patchfleet.profiles import CATALOGUE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "PatchFleet Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("# Test project\n\nA local CLI.\n", encoding="utf-8")
    (repo / "CONTRIBUTING.md").write_text(
        "# Contributing\n\n- Add tests for changes.\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    source = repo / "src"
    source.mkdir()
    (source / "tool.py").write_text(
        "def public_command(value: str) -> str:\n    return value\n", encoding="utf-8"
    )
    charter = {
        "schema_version": "0.1",
        "technology_stack": {"languages": ["python"], "frameworks": ["typer"]},
        "profiles": ["python-cli"],
        "architecture": {"style": "modular-monolith", "forbidden_patterns": ["shell_true"]},
        "quality": {
            "required_checks": ["python -m pytest"],
            "require_tests": True,
            "require_type_hints": True,
            "require_documented_public_cli": True,
            "require_documentation": True,
            "require_api_contracts": False,
        },
        "risk_policy": {
            "require_migration_plan": False,
            "require_rollback_for_schema_change": False,
        },
        "security_sensitivity": "normal",
        "non_negotiable_rules": ["Use explicit model assignments."],
    }
    (repo / "patchfleet.project.yaml").write_text(yaml.safe_dump(charter), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo


def dossier_data(repository: Path, plan_data: dict) -> dict:
    charter = load_charter(repository)
    context = inspect_context(repository, ("src/tool.py",))
    persist_context(context)
    evidence = next(item for item in context.evidence if item.locator.path == "README.md")
    plan_data["leader"]["assigned_provider"] = "user-leader-provider"
    plan_data["leader"]["selected_model"] = "user-leader-model"
    plan_data["tasks"][0]["allowed_paths"] = ["src/tool.py"]
    plan = copy.deepcopy(plan_data)
    compliance = {
        rule_id: "We keep the task within the approved scope."
        for rule_id in charter_rule_ids(charter)
        if rule_id.startswith(
            ("charter:architecture.forbidden_patterns", "charter:non_negotiable_rules")
        )
    }
    return {
        "schema_version": "0.1",
        "dossier_id": "dossier-1",
        "context_id": context.context_id,
        "base_commit": context.base_commit,
        "charter_fingerprint": charter_fingerprint(charter),
        "profile_versions": {profile: CATALOGUE[profile].version for profile in charter.profiles},
        "user_request": "Add a safe command.",
        "non_goals": ["No remote actions."],
        "change_risk": "low",
        "leader": plan["leader"],
        "assumptions": [],
        "unresolved_questions": [],
        "repository_findings": [
            {
                "statement": "The repository has CLI documentation.",
                "evidence": [{"source": "repository", "reference_id": evidence.evidence_id}],
            }
        ],
        "architecture_style": charter.architecture.style,
        "architecture_decisions": [
            {
                "decision_id": "D1",
                "statement": "Keep the CLI modular.",
                "rationale": "The repository already has a small CLI structure.",
                "alternatives": [
                    {"option": "One large module", "trade_off": "Less separation of concerns."}
                ],
                "evidence": [{"source": "repository", "reference_id": evidence.evidence_id}],
                "approval_basis": "none",
            }
        ],
        "charter_compliance": compliance,
        "contract_changes": [
            {
                "kind": "module",
                "description": "Add a command module.",
                "compatibility": "Existing commands remain.",
            }
        ],
        "considerations": {
            "security": "No new privilege boundary.",
            "reliability": "Failures remain visible.",
            "performance": "Small local operation.",
            "documentation": "Document the public CLI.",
            "api_contracts": "The command signature is explicit.",
            "tests": "Exercise success and failure exits.",
        },
        "quality_commitments": {
            "planned_checks": ["python -m pytest"],
            "tests": "Add focused tests.",
            "type_hints": "Type public functions.",
            "documented_public_cli": "Update command help and README.",
            "documentation": "Update README.",
            "api_contracts": "Document command inputs and exits.",
        },
        "test_strategy": "Test CLI success and error paths with temporary repositories.",
        "risks": [],
        "recommended_dependencies": [{"task_id": "T1", "dependencies": []}],
        "capability_claims": [],
        "proposed_plan": plan,
    }


def codes(error: ArchitectureGateError) -> set[str]:
    return {issue.code for issue in error.issues}


def test_charter_requires_explicit_known_profiles(repository: Path) -> None:
    charter = load_charter(repository)
    assert charter.profiles == ("python-cli",)
    data = charter.model_dump(mode="json")
    data["profiles"] = ["unknown-profile"]
    with pytest.raises(CharterValidationError) as raised:
        validate_charter(data)
    assert raised.value.issues[0].code == "unknown_profile"
    data["profiles"] = []
    assert validate_charter(data).profiles == ()  # No profile is inferred.
    data["quality"]["require_tests"] = "yes"
    with pytest.raises(CharterValidationError):
        validate_charter(data)


def test_charter_init_is_explicit_and_never_overwrites(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "PatchFleet Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "commit", "--allow-empty", "-qm", "baseline")
    runner = CliRunner()
    head = git(repo, "rev-parse", "HEAD")
    inspection = runner.invoke(app, ["context", "inspect", "--repo", str(repo)])
    assert inspection.exit_code == 0
    assert not (repo / "patchfleet.project.yaml").exists()
    created = runner.invoke(app, ["charter", "init", "--repo", str(repo)])
    assert created.exit_code == 0
    path = repo / "patchfleet.project.yaml"
    original = path.read_text()
    assert "No profile is selected" in original
    again = runner.invoke(app, ["charter", "init", "--repo", str(repo)])
    assert again.exit_code != 0 and path.read_text() == original
    with pytest.raises(CharterValidationError, match="track the charter"):
        load_charter(repo)
    assert git(repo, "rev-parse", "HEAD") == head


def test_context_excludes_secrets_ignored_binary_and_oversized(repository: Path) -> None:
    (repository / ".env").write_text("API_KEY=hidden-value\n", encoding="utf-8")
    (repository / "secrets.txt").write_text("hidden-value\n", encoding="utf-8")
    (repository / "big.md").write_text("x" * 70000, encoding="utf-8")
    (repository / "binary.bin").write_bytes(b"a\0b")
    git(repository, "add", "-f", ".env", "secrets.txt", "big.md", "binary.bin")
    git(repository, "commit", "-qm", "more tracked files")
    runtime = repository / ".patchfleet"
    runtime.mkdir()
    (runtime / ".gitignore").write_text("*\n", encoding="utf-8")
    (runtime / "ignored.md").write_text("# hidden-value\n", encoding="utf-8")
    first = inspect_context(repository, ("src/tool.py",))
    second = inspect_context(repository, ("src/tool.py",))
    assert first.context_id == second.context_id
    paths = {item.locator.path for item in first.evidence}
    assert "src/tool.py" in paths
    assert not paths.intersection(
        {".env", "secrets.txt", "big.md", "binary.bin", ".patchfleet/ignored.md"}
    )
    assert "hidden-value" not in str(first.model_dump())
    for item in first.evidence:
        assert item.locator.start_line > 0 and item.locator.end_line >= item.locator.start_line
    path = persist_context(first)
    assert load_context(repository, first.context_id) == first
    shown = CliRunner().invoke(
        app, ["context", "show", first.context_id, "--repo", str(repository)]
    )
    assert shown.exit_code == 0 and "hidden-value" not in shown.stdout
    assert path.is_file()
    with pytest.raises(ContextError):
        inspect_context(repository, (".env",))


def test_context_refuses_tracked_path_redirected_through_symlink(
    repository: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tool.py").write_text("def leaked_secret(): pass\n", encoding="utf-8")
    (repository / "src").rename(repository / "src-backup")
    (repository / "src").symlink_to(outside, target_is_directory=True)
    context = inspect_context(repository)
    assert all(item.locator.path != "src/tool.py" for item in context.evidence)
    assert "leaked_secret" not in str(context.model_dump())


def test_ready_dossier_passes_gate_without_approval(repository: Path, plan_data: dict) -> None:
    data = dossier_data(repository, plan_data)
    assert validate_dossier(data, repository).dossier_id == "dossier-1"
    path = repository.parent / "dossier.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["architecture", "validate", str(path), "--repo", str(repository)]
    )
    assert result.exit_code == 0
    assert "separate validation and explicit execution approval" in result.stdout
    assert not (repository / ".patchfleet" / "patchfleet.sqlite3").exists()


def test_missing_profile_sections_and_decision_evidence(repository: Path, plan_data: dict) -> None:
    data = dossier_data(repository, plan_data)
    data["considerations"].pop("documentation")
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "profile_section_required" in codes(raised.value)
    data = dossier_data(repository, plan_data)
    data["architecture_decisions"][0]["evidence"] = []
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert any("architecture_decisions" in issue.path for issue in raised.value.issues)


def test_unknown_decision_evidence_and_agent_approval_rejected(
    repository: Path, plan_data: dict
) -> None:
    data = dossier_data(repository, plan_data)
    data["architecture_decisions"][0]["evidence"][0]["reference_id"] = "repo:made-up"
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "unknown_evidence" in codes(raised.value)
    data = dossier_data(repository, plan_data)
    data["architecture_decisions"][0]["approval_basis"] = "agent_message"
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert any("approval_basis" in issue.path for issue in raised.value.issues)


def test_plan_invalid_dossier_rejected(repository: Path, plan_data: dict) -> None:
    data = dossier_data(repository, plan_data)
    data["proposed_plan"]["tasks"][0]["dependencies"] = ["missing"]
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "missing_dependency" in codes(raised.value)


def test_gate_requires_charter_checks_and_exact_task_graph(
    repository: Path, plan_data: dict
) -> None:
    data = dossier_data(repository, plan_data)
    data["quality_commitments"]["planned_checks"] = []
    data["recommended_dependencies"][0]["dependencies"] = ["unexpected"]
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert {"required_check_missing", "graph_mismatch"}.issubset(codes(raised.value))


def test_database_profile_requires_migration_and_rollback(
    repository: Path, plan_data: dict
) -> None:
    charter_path = repository / "patchfleet.project.yaml"
    charter = yaml.safe_load(charter_path.read_text())
    charter["profiles"] = ["database-change"]
    charter["risk_policy"]["require_migration_plan"] = True
    charter["risk_policy"]["require_rollback_for_schema_change"] = True
    charter_path.write_text(yaml.safe_dump(charter), encoding="utf-8")
    git(repository, "add", "patchfleet.project.yaml")
    git(repository, "commit", "-qm", "database policy")
    data = dossier_data(repository, plan_data)
    data["contract_changes"] = [
        {
            "kind": "data",
            "description": "Add a table.",
            "compatibility": "Old code reads existing columns.",
        }
    ]
    data["considerations"].pop("migration", None)
    data["considerations"].pop("rollback", None)
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    paths = {issue.path for issue in raised.value.issues}
    assert "considerations.migration" in paths
    assert "considerations.rollback" in paths
    data["considerations"].update(
        {"migration": "Apply additive migration.", "rollback": "Revert schema after data backup."}
    )
    assert validate_dossier(data, repository).dossier_id == "dossier-1"


def test_high_risk_requires_attention_and_charter_treatment(
    repository: Path, plan_data: dict
) -> None:
    charter_path = repository / "patchfleet.project.yaml"
    charter = yaml.safe_load(charter_path.read_text())
    charter["risk_policy"]["require_migration_plan"] = True
    charter_path.write_text(yaml.safe_dump(charter), encoding="utf-8")
    git(repository, "add", "patchfleet.project.yaml")
    git(repository, "commit", "-qm", "high risk policy")
    data = dossier_data(repository, plan_data)
    data["change_risk"] = "high"
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "high_risk_treatment_required" in codes(raised.value)
    assert any(issue.path == "considerations.migration" for issue in raised.value.issues)


def test_security_profile_requires_user_attention_and_rollback(
    repository: Path, plan_data: dict
) -> None:
    charter_path = repository / "patchfleet.project.yaml"
    charter = yaml.safe_load(charter_path.read_text())
    charter["profiles"] = ["security-sensitive"]
    charter["security_sensitivity"] = "high"
    charter_path.write_text(yaml.safe_dump(charter), encoding="utf-8")
    git(repository, "add", "patchfleet.project.yaml")
    git(repository, "commit", "-qm", "sensitive policy")
    data = dossier_data(repository, plan_data)
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "risk_attention_required" in codes(raised.value)
    assert any(issue.path == "considerations.rollback" for issue in raised.value.issues)
    data["considerations"]["rollback"] = "Restore the previous configuration."
    data["risks"] = [
        {
            "severity": "medium",
            "description": "A command may expose data.",
            "mitigation": "Restrict output and test redaction.",
            "requires_user_attention": True,
            "evidence": data["repository_findings"][0]["evidence"],
        }
    ]
    assert validate_dossier(data, repository).dossier_id == "dossier-1"


def test_stale_context_and_unsupported_capability_rejected(
    repository: Path, plan_data: dict
) -> None:
    data = dossier_data(repository, plan_data)
    data["capability_claims"] = [
        {
            "capability": "reviewer_execution",
            "status": "validated",
            "evidence": data["repository_findings"][0]["evidence"],
        }
    ]
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "unsupported_capability" in codes(raised.value)
    data["capability_claims"] = []
    (repository / "README.md").write_text("# Changed project\n", encoding="utf-8")
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert "stale_context" in codes(raised.value)


def test_unresolved_questions_placeholders_and_knowledge_citations_block_readiness(
    repository: Path, plan_data: dict
) -> None:
    data = dossier_data(repository, plan_data)
    data["unresolved_questions"] = ["Which Worker model should be used?"]
    data["proposed_plan"]["tasks"][0]["selected_model"] = "<user-selected-model>"
    data["architecture_decisions"][0]["evidence"] = [
        {"source": "knowledge", "reference_id": "knowledge:not-yet-supported"}
    ]
    with pytest.raises(ArchitectureGateError) as raised:
        validate_dossier(data, repository)
    assert {"clarification_required", "user_selection_required", "unknown_evidence"}.issubset(
        codes(raised.value)
    )


def test_prompt_compilation_pins_user_selection_and_invokes_nothing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = tmp_path / "request.md"
    request.write_text("Add a command; do not push.\n", encoding="utf-8")
    original_run = subprocess.run
    commands: list[list[str]] = []

    def read_only_git(command: list[str], *args: object, **kwargs: object):
        commands.append(command)
        assert command[0] == "git"
        assert not any(
            item in command for item in ("add", "commit", "worktree", "push", "merge", "apply")
        )
        return original_run(command, *args, **kwargs)

    async def forbidden_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no model or Worker may be invoked")

    monkeypatch.setattr(subprocess, "run", read_only_git)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_process)
    head = git(repository, "rev-parse", "HEAD")
    result = CliRunner().invoke(
        app,
        [
            "leader",
            "prompt",
            str(request),
            "--repo",
            str(repository),
            "--provider",
            "user-provider",
            "--model",
            "user-model",
            "--path",
            "src/tool.py",
        ],
    )
    assert result.exit_code == 0
    assert "No model was invoked" in result.stdout
    prompt_path = Path(result.stdout.splitlines()[0].removeprefix("Local prompt: "))
    content = prompt_path.read_text(encoding="utf-8")
    assert "Leader provider: user-provider" in content
    assert "Leader model: user-model" in content
    assert "PlanningDossier JSON Schema" in content
    assert "Ask the user for clarification" in content
    assert "repo:" in content
    repeated = CliRunner().invoke(
        app,
        [
            "leader",
            "prompt",
            str(request),
            "--repo",
            str(repository),
            "--provider",
            "user-provider",
            "--model",
            "user-model",
            "--path",
            "src/tool.py",
        ],
    )
    assert repeated.exit_code == 0 and str(prompt_path) in repeated.stdout
    alternate = CliRunner().invoke(
        app,
        [
            "leader",
            "prompt",
            str(request),
            "--repo",
            str(repository),
            "--provider",
            "user-provider",
            "--model",
            "other-user-model",
            "--path",
            "src/tool.py",
        ],
    )
    assert alternate.exit_code == 0
    alternate_path = Path(alternate.stdout.splitlines()[0].removeprefix("Local prompt: "))
    assert alternate_path != prompt_path
    assert "Leader model: other-user-model" in alternate_path.read_text(encoding="utf-8")
    missing_choice = CliRunner().invoke(
        app,
        [
            "leader",
            "prompt",
            str(request),
            "--repo",
            str(repository),
            "--provider",
            "user-provider",
            "--model",
            "auto",
        ],
    )
    assert missing_choice.exit_code != 0
    assert git(repository, "rev-parse", "HEAD") == head
    assert git(repository, "status", "--porcelain") == ""
    assert not list((repository / ".patchfleet").rglob("*.jsonl"))
    assert commands
