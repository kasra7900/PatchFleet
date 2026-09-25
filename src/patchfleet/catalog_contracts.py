"""Provider-owned live model catalog contracts.

A catalog is a point-in-time result of asking an installed, already-authenticated
provider CLI what models it can currently reach. PatchFleet never ships a curated
or fallback model list; a model is selectable only from a fresh successful result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel


class ModelCatalogSource(StrEnum):
    CODEX_APP_SERVER = "codex-app-server"
    OPENCODE_CLI = "opencode-cli"
    CLAUDE_CLI = "claude-cli"
    UNAVAILABLE = "unavailable"


class ModelCatalogStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class DiscoveredModel(StrictModel):
    """One model reported by a provider's own local catalog."""

    model_id: str
    display_name: str
    description: str | None = None
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    hidden: bool = False
    is_default: bool = False
    access_note: str | None = None

    @field_validator("model_id", "display_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model id and display name must not be blank")
        return value

    @field_validator("reasoning_efforts")
    @classmethod
    def nonblank_efforts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("reasoning effort values must not be blank")
        return cleaned

    def supports_effort(self, effort: str) -> bool:
        return effort in self.reasoning_efforts


class ModelCatalog(StrictModel):
    """The full result of one provider catalog refresh."""

    provider_id: str
    status: ModelCatalogStatus
    source: ModelCatalogSource
    discovered_at: datetime
    verified_available: bool
    diagnostics: tuple[str, ...] = ()
    models: tuple[DiscoveredModel, ...] = ()
    pages: int = Field(default=0, ge=0)
    truncated: bool = False

    @field_validator("discovered_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("discovered_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("provider_id")
    @classmethod
    def nonblank_provider(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("provider id must not be blank")
        return value

    @model_validator(mode="after")
    def consistent_status(self) -> ModelCatalog:
        if self.status == ModelCatalogStatus.OK:
            if self.verified_available and not self.models:
                raise ValueError("a verified catalog must list at least one visible model")
        elif self.verified_available:
            raise ValueError("only an OK catalog can be verified available")
        return self

    def visible_models(self) -> tuple[DiscoveredModel, ...]:
        return tuple(model for model in self.models if not model.hidden)

    def model(self, model_id: str) -> DiscoveredModel | None:
        return next((model for model in self.models if model.model_id == model_id), None)


class ProviderCapabilities(StrictModel):
    """User-facing provider capability summary with catalog status."""

    provider_id: str
    display_name: str
    installed: bool
    authenticated: bool | None = None
    leader_capable: bool
    worker_capable: bool
    catalog_available: bool
    catalog_status: ModelCatalogStatus
    catalog_source: ModelCatalogSource
    diagnostics: tuple[str, ...] = ()


def unavailable_catalog(
    provider_id: str,
    source: ModelCatalogSource,
    *diagnostics: str,
    status: ModelCatalogStatus = ModelCatalogStatus.UNAVAILABLE,
) -> ModelCatalog:
    return ModelCatalog(
        provider_id=provider_id,
        status=status,
        source=source,
        discovered_at=datetime.now(UTC),
        verified_available=False,
        diagnostics=tuple(diagnostics),
    )
