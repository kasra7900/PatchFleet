"""Provider-owned live model catalogs.

Each adapter asks one installed, already-authenticated provider CLI what models
it can currently reach. There is no curated fallback list anywhere: a catalog is
either a fresh live result or an explicit unavailable/error status.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

from .catalog_contracts import (
    DiscoveredModel,
    ModelCatalog,
    ModelCatalogSource,
    ModelCatalogStatus,
    ProviderCapabilities,
    unavailable_catalog,
)
from .discovery import ProviderStatus
from .processes import supervise

DEFAULT_CATALOG_TIMEOUT = 30.0
MAX_MODELS = 5000
MAX_LINE_BYTES = 1_048_576


class _CatalogProtocolError(ValueError):
    """A provider catalog response cannot be trusted or parsed."""


class _CatalogTimeout(TimeoutError):
    pass


class _CatalogCancelled(asyncio.CancelledError):
    pass


class CatalogAdapter:
    provider_id: str
    source: ClassVar[ModelCatalogSource]

    def __init__(self, executable: Path) -> None:
        self.executable = executable

    async def fetch(
        self, *, timeout: float = DEFAULT_CATALOG_TIMEOUT, cancel_event: asyncio.Event | None = None
    ) -> ModelCatalog:
        raise NotImplementedError


def _read_object_end(text: str, start: int) -> int:
    """Return the index just after a balanced JSON object beginning at ``start``."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def parse_opencode_verbose(text: str) -> tuple[DiscoveredModel, ...]:
    """Parse the joined ``provider/model`` + JSON blocks of ``opencode models --verbose``."""
    models: list[DiscoveredModel] = []
    position = 0
    length = len(text)
    while position < length:
        line_end = text.find("\n", position)
        line_end = length if line_end == -1 else line_end
        line = text[position:line_end].strip()
        position = line_end + 1
        if not line or line.startswith("{") or any(character.isspace() for character in line):
            continue
        if "/" not in line:
            continue
        brace = text.find("{", position)
        if brace == -1:
            break
        end = _read_object_end(text, brace)
        if end == -1:
            break
        try:
            data = json.loads(text[brace:end])
        except json.JSONDecodeError:
            position = end
            continue
        position = end
        if isinstance(data, dict):
            models.append(_opencode_model(line, data))
    return tuple(models)


def _opencode_model(header: str, data: Mapping[str, Any]) -> DiscoveredModel:
    model_id = header.strip()
    raw_id = data.get("id")
    if isinstance(raw_id, str) and "/" not in model_id:
        model_id = raw_id.strip() or model_id
    name = data.get("name")
    display_name = name.strip() if isinstance(name, str) and name.strip() else model_id
    variants = data.get("variants")
    efforts: list[str] = []
    if isinstance(variants, Mapping):
        for key, value in variants.items():
            effort = value.get("reasoningEffort") if isinstance(value, Mapping) else None
            candidate = effort if isinstance(effort, str) and effort.strip() else key
            if isinstance(candidate, str) and candidate.strip() and candidate not in efforts:
                efforts.append(candidate.strip())
    status = data.get("status")
    hidden = isinstance(status, str) and status.strip().lower() not in {"", "active"}
    description = data.get("family") if isinstance(data.get("family"), str) else None
    return DiscoveredModel(
        model_id=model_id,
        display_name=display_name,
        description=description,
        reasoning_efforts=tuple(efforts),
        hidden=hidden,
    )


class OpenCodeCatalogAdapter(CatalogAdapter):
    provider_id = "opencode"
    source = ModelCatalogSource.OPENCODE_CLI

    async def fetch(
        self, *, timeout: float = DEFAULT_CATALOG_TIMEOUT, cancel_event: asyncio.Event | None = None
    ) -> ModelCatalog:
        try:
            result = await supervise(
                [str(self.executable), "models", "--verbose"],
                timeout=timeout,
                max_output_bytes=2_000_000,
                cancel_event=cancel_event,
            )
        except (OSError, ValueError) as error:
            return unavailable_catalog(
                self.provider_id,
                self.source,
                f"could not run opencode models: {error}",
                status=ModelCatalogStatus.ERROR,
            )
        if result.cancelled:
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "opencode catalog was cancelled",
                status=ModelCatalogStatus.ERROR,
            )
        if result.timed_out:
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "opencode catalog timed out",
                status=ModelCatalogStatus.ERROR,
            )
        if result.status != "succeeded":
            return unavailable_catalog(
                self.provider_id,
                self.source,
                f"opencode models exited with status {result.exit_code}",
                status=ModelCatalogStatus.ERROR,
            )
        if result.output_truncated:
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "opencode model output exceeded the bounded local limit",
                status=ModelCatalogStatus.ERROR,
            )
        text = (result.stdout + result.stderr).decode("utf-8", "replace")
        try:
            models = parse_opencode_verbose(text)
        except (ValueError, RecursionError):
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "opencode catalog output was malformed",
                status=ModelCatalogStatus.ERROR,
            )
        visible = tuple(model for model in models if not model.hidden)
        if not visible:
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "opencode reported no active models for the connected providers",
            )
        return ModelCatalog(
            provider_id=self.provider_id,
            status=ModelCatalogStatus.OK,
            source=self.source,
            discovered_at=_now(),
            verified_available=True,
            models=models,
            pages=1,
        )


class ClaudeCatalogAdapter(CatalogAdapter):
    provider_id = "claude-code"
    source = ModelCatalogSource.CLAUDE_CLI

    _CATALOG_COMMAND_TOKENS: ClassVar[tuple[str, ...]] = ("models", "--list-models")

    async def fetch(
        self, *, timeout: float = DEFAULT_CATALOG_TIMEOUT, cancel_event: asyncio.Event | None = None
    ) -> ModelCatalog:
        # Claude Code documents aliases and explicit model ids but no account-specific
        # catalog command. We do not invent one and we do not ship a static list.
        try:
            help_result = await supervise(
                [str(self.executable), "--help"],
                timeout=min(timeout, 10.0),
                max_output_bytes=32768,
                cancel_event=cancel_event,
            )
        except (OSError, ValueError):
            help_result = None
        help_text = ""
        if help_result is not None and help_result.stdout:
            help_text = (help_result.stdout + help_result.stderr).decode("utf-8", "replace")
        if any(token in help_text for token in self._CATALOG_COMMAND_TOKENS):
            return unavailable_catalog(
                self.provider_id,
                self.source,
                "This Claude Code version appears to expose a catalog command, but PatchFleet "
                "does not yet implement or test an account catalog adapter for it.",
            )
        return unavailable_catalog(
            self.provider_id,
            self.source,
            "Claude Code does not document an account-specific model catalog command. "
            "PatchFleet will not invent one or ship a static Claude list.",
        )


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


class _CodexAppServer:
    """Bounded newline-delimited JSON-RPC client for ``codex app-server --stdio``."""

    def __init__(self, executable: Path, *, stderr_cap: int = 32768) -> None:
        self.executable = executable
        self.stderr_cap = stderr_cap
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_bytes = 0
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 0

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_LINE_BYTES,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while chunk := await self.process.stderr.read(65536):
                self._stderr_bytes = min(self._stderr_bytes + len(chunk), self.stderr_cap)
        except (OSError, ValueError):
            return

    async def _write(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        await self.process.stdin.drain()

    def _request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _readline(self, deadline: float, cancel_event: asyncio.Event | None) -> str:
        assert self.process is not None and self.process.stdout is not None
        read_task = asyncio.ensure_future(self.process.stdout.readline())
        cancel_task = (
            asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        )
        tasks = {read_task, cancel_task} if cancel_task is not None else {read_task}
        remaining = deadline - monotonic()
        if remaining <= 0:
            for task in tasks:
                task.cancel()
            raise _CatalogTimeout("catalog deadline exceeded")
        done, _ = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        try:
            if read_task in done:
                return read_task.result().decode("utf-8", "replace").strip()
            if cancel_task is not None and cancel_task in done:
                raise _CatalogCancelled("catalog was cancelled")
            raise _CatalogTimeout("catalog timed out waiting for the provider")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> Any:
        request_id = self._request_id()
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)
        while True:
            line = await self._readline(deadline, cancel_event)
            if not line:
                raise _CatalogProtocolError("the app-server closed before responding")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise _CatalogProtocolError("the app-server sent malformed JSON") from error
            if not isinstance(payload, dict):
                continue
            if payload.get("id") != request_id:
                continue  # ignore interleaved notifications
            if "error" in payload:
                raise _CatalogProtocolError("the app-server returned a model/list error")
            return payload.get("result")

    async def notify(self, method: str) -> None:
        await self._write({"jsonrpc": "2.0", "method": method})

    async def shutdown(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        except (OSError, ValueError):
            pass
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), 15)
                else:
                    process.terminate()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(process.pid), 9)
                    else:
                        process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)


def _codex_model(item: object, diagnostics: list[str]) -> DiscoveredModel | None:
    if not isinstance(item, dict):
        diagnostics.append("skipped a malformed model entry")
        return None
    model_id = item.get("model") or item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        diagnostics.append("skipped a model entry without an id")
        return None
    display = item.get("displayName")
    display_name = display.strip() if isinstance(display, str) and display.strip() else model_id
    description = item.get("description") if isinstance(item.get("description"), str) else None
    efforts: list[str] = []
    options = item.get("supportedReasoningEfforts")
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict) and isinstance(option.get("reasoningEffort"), str):
                value = option["reasoningEffort"].strip()
                if value and value not in efforts:
                    efforts.append(value)
    default = item.get("defaultReasoningEffort")
    default_effort = default.strip() if isinstance(default, str) and default.strip() else None
    access_note = None
    nux = item.get("availabilityNux")
    if isinstance(nux, dict) and isinstance(nux.get("message"), str):
        access_note = nux["message"].strip() or None
    return DiscoveredModel(
        model_id=model_id.strip(),
        display_name=display_name,
        description=description,
        reasoning_efforts=tuple(efforts),
        default_reasoning_effort=default_effort,
        hidden=bool(item.get("hidden", False)),
        is_default=bool(item.get("isDefault", False)),
        access_note=access_note,
    )


class CodexCatalogAdapter(CatalogAdapter):
    provider_id = "codex-cli"
    source = ModelCatalogSource.CODEX_APP_SERVER
    max_pages: ClassVar[int] = 50

    async def fetch(
        self, *, timeout: float = DEFAULT_CATALOG_TIMEOUT, cancel_event: asyncio.Event | None = None
    ) -> ModelCatalog:
        deadline = monotonic() + timeout
        diagnostics: list[str] = []
        models: list[DiscoveredModel] = []
        pages = 0
        truncated = False
        client = _CodexAppServer(self.executable)
        status = ModelCatalogStatus.OK
        try:
            await client.start()
            await client.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "patchfleet",
                        "title": "PatchFleet",
                        "version": "0.0.0",
                    }
                },
                deadline,
                cancel_event,
            )
            await client.notify("initialized")
            cursor: str | None = None
            while True:
                if pages >= self.max_pages:
                    diagnostics.append("model catalog pagination limit reached")
                    truncated = True
                    break
                result = await client.request(
                    "model/list",
                    {"cursor": cursor, "limit": 100},
                    deadline,
                    cancel_event,
                )
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise _CatalogProtocolError("model/list returned an unexpected shape")
                for entry in result["data"]:
                    model = _codex_model(entry, diagnostics)
                    if model is not None:
                        models.append(model)
                pages += 1
                next_cursor = result.get("nextCursor")
                cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
                if cursor is None:
                    break
                if len(models) >= MAX_MODELS:
                    diagnostics.append("model catalog size limit reached")
                    truncated = True
                    break
        except _CatalogCancelled:
            status = ModelCatalogStatus.ERROR
            diagnostics.append("catalog was cancelled")
        except _CatalogTimeout:
            status = ModelCatalogStatus.ERROR
            diagnostics.append("catalog timed out")
        except _CatalogProtocolError as error:
            status = ModelCatalogStatus.ERROR
            diagnostics.append(str(error))
        except (OSError, ValueError) as error:
            status = ModelCatalogStatus.ERROR
            diagnostics.append(f"could not start the local app-server: {error}")
        finally:
            await client.shutdown()
        visible = tuple(model for model in models if not model.hidden)
        if status == ModelCatalogStatus.OK and not visible:
            status = ModelCatalogStatus.UNAVAILABLE
            diagnostics.append("the app-server reported no visible models for this account")
        return ModelCatalog(
            provider_id=self.provider_id,
            status=status,
            source=self.source,
            discovered_at=_now(),
            verified_available=status == ModelCatalogStatus.OK and bool(visible),
            diagnostics=tuple(diagnostics),
            models=tuple(models),
            pages=pages,
            truncated=truncated,
        )


CATALOG_ADAPTERS: dict[str, type[CatalogAdapter]] = {
    "codex-cli": CodexCatalogAdapter,
    "opencode": OpenCodeCatalogAdapter,
    "claude-code": ClaudeCatalogAdapter,
}
CATALOG_SOURCES: dict[str, ModelCatalogSource] = {
    provider_id: adapter.source for provider_id, adapter in CATALOG_ADAPTERS.items()
}


@dataclass(frozen=True)
class CatalogFetch:
    provider_id: str
    executable: Path


async def fetch_catalog(
    provider_id: str,
    executable: Path,
    *,
    timeout: float = DEFAULT_CATALOG_TIMEOUT,
    cancel_event: asyncio.Event | None = None,
) -> ModelCatalog:
    adapter_class = CATALOG_ADAPTERS.get(provider_id)
    if adapter_class is None:
        return unavailable_catalog(
            provider_id, ModelCatalogSource.UNAVAILABLE, "no catalog adapter for this provider"
        )
    return await adapter_class(executable).fetch(timeout=timeout, cancel_event=cancel_event)


def fetch_catalog_sync(
    provider_id: str,
    executable: Path,
    *,
    timeout: float = DEFAULT_CATALOG_TIMEOUT,
    cancel_event: asyncio.Event | None = None,
) -> ModelCatalog:
    try:
        return asyncio.run(
            fetch_catalog(provider_id, executable, timeout=timeout, cancel_event=cancel_event)
        )
    except KeyboardInterrupt:
        return unavailable_catalog(
            provider_id,
            CATALOG_SOURCES.get(provider_id, ModelCatalogSource.UNAVAILABLE),
            "catalog was cancelled",
            status=ModelCatalogStatus.ERROR,
        )


def provider_capabilities(
    statuses: tuple[ProviderStatus, ...],
    catalogs: Mapping[str, ModelCatalog] | None = None,
) -> tuple[ProviderCapabilities, ...]:
    catalogs = catalogs or {}
    capabilities: list[ProviderCapabilities] = []
    for status in statuses:
        catalog = catalogs.get(status.provider_id)
        source = (
            catalog.source
            if catalog is not None
            else CATALOG_SOURCES.get(status.provider_id, ModelCatalogSource.UNAVAILABLE)
        )
        catalog_status = catalog.status if catalog is not None else ModelCatalogStatus.UNAVAILABLE
        diagnostics = list(catalog.diagnostics) if catalog is not None else []
        if status.provider_id not in CATALOG_ADAPTERS:
            diagnostics.append("no local catalog adapter for this provider")
        capabilities.append(
            ProviderCapabilities(
                provider_id=status.provider_id,
                display_name=status.display_name,
                installed=status.installed,
                authenticated=None,
                leader_capable=status.leader_capable,
                worker_capable=status.execution_capable,
                catalog_available=catalog_status == ModelCatalogStatus.OK
                and bool(catalog and catalog.verified_available),
                catalog_status=catalog_status,
                catalog_source=source,
                diagnostics=tuple(diagnostics),
            )
        )
    return tuple(capabilities)
