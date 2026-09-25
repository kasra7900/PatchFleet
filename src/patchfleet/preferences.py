"""Versioned, non-secret personal defaults stored outside the target repository.

Preferences are deliberately separate from target-repository ``.patchfleet/``
state, the tracked ``patchfleet.project.yaml`` charter, the tracked
``patchfleet.knowledge.yaml`` registry, and execution approval records. They
never contain credentials, prompts, raw outputs, tokens, environment variables,
approvals, or repository task state.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from platformdirs import user_config_dir
from pydantic import Field, StrictInt, ValidationError, field_validator

from .contracts import StrictModel


class PreferencesError(ValueError):
    """Stored personal preferences are unreadable or invalid."""


class Selection(StrictModel):
    """An explicit user choice of provider and model; never inferred.

    ``catalog_source`` and ``catalog_discovered_at`` record which live provider
    catalog the choice came from. They never carry credentials or raw catalog
    data, and older preference files without them remain valid.
    """

    provider: str
    model: str
    reasoning_effort: str | None = None
    catalog_source: str | None = None
    catalog_discovered_at: datetime | None = None

    @field_validator("provider", "model")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("provider and model selections must not be blank")
        return value

    @field_validator("reasoning_effort", "catalog_source")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("catalog_discovered_at")
    @classmethod
    def utc_catalog_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog discovery time must include a timezone")
        return value.astimezone(UTC)


class UiPreferences(StrictModel):
    """Small presentation choices that this phase actually honors."""

    show_provider_status: bool = True


class UserPreferences(StrictModel):
    """Personal, non-secret defaults for the interactive experience."""

    schema_version: Literal["0.1"]
    provider_executables: dict[str, str] = Field(default_factory=dict)
    leader: Selection
    workers: tuple[Selection, ...] = Field(min_length=1)
    max_parallel_workers: StrictInt = Field(gt=0)
    ui: UiPreferences = Field(default_factory=UiPreferences)

    @field_validator("provider_executables")
    @classmethod
    def nonblank_executables(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for provider, executable in value.items():
            provider = provider.strip()
            executable = executable.strip()
            if not provider or not executable:
                raise ValueError("provider executable overrides must not be blank")
            cleaned[provider] = executable
        return cleaned


def user_preferences_path() -> Path:
    """Return the platform-appropriate personal preferences file location."""
    return Path(user_config_dir("patchfleet", appauthor=False)) / "preferences.yaml"


def load_preferences(path: Path | None = None) -> UserPreferences | None:
    """Load preferences, or return ``None`` when none have been created.

    A present but malformed file raises :class:`PreferencesError`; it is never
    silently rewritten or replaced with defaults.
    """
    target = path or user_preferences_path()
    if not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8") as source:
            document = yaml.safe_load(source)
        return UserPreferences.model_validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise PreferencesError(f"invalid preferences at {target}: {error}") from error


def save_preferences(preferences: UserPreferences, path: Path | None = None) -> Path:
    """Atomically write preferences with restrictive permissions where supported."""
    target = path or user_preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        preferences.model_dump(mode="json"), sort_keys=True, allow_unicode=True
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target
