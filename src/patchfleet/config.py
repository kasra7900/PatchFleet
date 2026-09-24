"""Explicit project-local execution configuration; no provider is enabled by default."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from .contracts import StrictModel


class ConfigError(ValueError):
    """The local configuration is missing or unusable."""


class ExecutionConfig(StrictModel):
    max_parallel_workers: int = Field(gt=0, strict=True)


class ProviderConfig(StrictModel):
    enabled: bool = Field(strict=True)
    executable: str

    @field_validator("executable")
    @classmethod
    def nonblank_executable(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("executable must not be blank")
        return value


class LocalConfig(StrictModel):
    schema_version: Literal["0.1"]
    execution: ExecutionConfig
    providers: dict[str, ProviderConfig]


def config_path(repository: Path) -> Path:
    return repository / ".patchfleet" / "config.yaml"


def load_config(repository: Path) -> LocalConfig:
    path = config_path(repository)
    if not path.is_file():
        raise ConfigError(f"missing configuration: {path}")
    try:
        with path.open("r", encoding="utf-8") as source:
            return LocalConfig.model_validate(yaml.safe_load(source))
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise ConfigError(f"invalid configuration at {path}: {error}") from error


def resolve_executable(repository: Path, configured: str) -> Path | None:
    """Resolve only the configured executable, never a substitute."""
    candidate = Path(configured)
    if candidate.is_absolute() or candidate.parent != Path("."):
        path = candidate if candidate.is_absolute() else repository / candidate
        path = path.resolve()
    else:
        found = shutil.which(configured)
        if found is None:
            return None
        path = Path(found).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return path
