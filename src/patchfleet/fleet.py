"""Headless controller for the interactive Fleet experience.

This module owns no provider or persistence logic of its own. It coordinates the
existing catalog, planning, preference, handoff, execution, and monitor layers
so the Textual UI can stay thin and so the flow is testable without a terminal.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import catalog as catalog_module
from . import handoff as handoff_module
from . import planning
from . import preferences as preferences_module
from .catalog import CATALOG_SOURCES
from .catalog_contracts import (
    ModelCatalog,
    ModelCatalogSource,
    ModelCatalogStatus,
    ProviderCapabilities,
    unavailable_catalog,
)
from .discovery import ProviderStatus, resolve_executable
from .leader_adapters import LEADER_ADAPTERS
from .monitor import FleetMonitor, FleetSnapshot
from .preferences import Selection, UiPreferences, UserPreferences
from .settings import SOURCE_USER, EffectiveSettings

LeaderRunner = Callable[[planning.PlanningSession], object]
CatalogFetcher = Callable[[str, bool], ModelCatalog]

RESERVED_INPUTS = frozenset(
    {"", "done", "d", "q", "quit", "exit", "/quit", "/exit", "back", "cancel"}
)


class FleetError(ValueError):
    """The requested fleet action is not allowed in the current state."""


@dataclass
class TranscriptEntry:
    role: Literal["user", "leader", "system"]
    text: str


class FleetController:
    """Session-local controller. Catalogs and conversation live in memory only."""

    def __init__(
        self,
        *,
        repository: Path | None,
        statuses: tuple[ProviderStatus, ...],
        settings: EffectiveSettings,
        preferences_path: Path | None = None,
        leader_runner: LeaderRunner | None = None,
        catalog_fetcher: CatalogFetcher | None = None,
        catalog_timeout: float = 30.0,
    ) -> None:
        self.repository = repository
        self.statuses = statuses
        self.settings = settings
        self.preferences_path = preferences_path
        self.leader_runner = leader_runner
        self.catalog_fetcher = catalog_fetcher
        self.catalog_timeout = catalog_timeout
        self.provider_overrides = {
            provider_id: executable
            for provider_id, executable in settings.provider_executables.items()
            if settings.provider_executable_sources.get(provider_id) == SOURCE_USER
        }
        self.catalogs: dict[str, ModelCatalog] = {}
        self.leader: Selection | None = None
        self.workers: list[Selection] = []
        self.max_parallel_workers = settings.max_parallel_workers
        self.session: planning.PlanningSession | None = None
        self.state = "SETUP"
        self.transcript: list[TranscriptEntry] = []
        self.approved: handoff_module.ApprovedPlan | None = None
        self.monitor: FleetMonitor | None = None
        self._cancel_requested = threading.Event()
        self._task_cancel_events: dict[str, threading.Event] = {}
        self._run_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- catalogs ----------------------------------------------------------

    def _status(self, provider_id: str) -> ProviderStatus | None:
        return next((status for status in self.statuses if status.provider_id == provider_id), None)

    def fetch_catalog(self, provider_id: str, *, refresh: bool = False) -> ModelCatalog:
        if not refresh and provider_id in self.catalogs:
            return self.catalogs[provider_id]
        if self.catalog_fetcher is not None:
            result = self.catalog_fetcher(provider_id, refresh)
        elif self.repository is None:
            result = unavailable_catalog(
                provider_id, ModelCatalogSource.UNAVAILABLE, "open PatchFleet in a repository"
            )
        else:
            configured = self.settings.provider_executables.get(provider_id, provider_id)
            executable = resolve_executable(configured, base=self.repository or Path.cwd())
            if executable is None:
                result = unavailable_catalog(
                    provider_id,
                    CATALOG_SOURCES.get(provider_id, ModelCatalogSource.UNAVAILABLE),
                    "the provider executable was not found",
                )
            else:
                result = catalog_module.fetch_catalog_sync(
                    provider_id, executable, timeout=self.catalog_timeout
                )
        self.catalogs[provider_id] = result
        return result

    def capabilities(self) -> tuple[ProviderCapabilities, ...]:
        return catalog_module.provider_capabilities(self.statuses, self.catalogs)

    def model_catalog_available(self, provider_id: str) -> bool:
        result = self.catalogs.get(provider_id)
        return bool(
            result
            and result.status == ModelCatalogStatus.OK
            and result.verified_available
            and result.visible_models()
        )

    # -- selection ---------------------------------------------------------

    def _validate_model(
        self,
        provider_id: str,
        model_id: str,
        reasoning_effort: str | None,
        *,
        role: str,
        refresh: bool,
    ) -> Selection:
        if model_id.strip().casefold() in RESERVED_INPUTS:
            raise FleetError(
                f"{model_id!r} is a reserved input and cannot be a model name; "
                "choose a model from a live catalog"
            )
        status = self._status(provider_id)
        if status is None or not status.installed:
            raise FleetError(f"provider {provider_id} is not installed")
        if role == "leader" and not status.leader_capable:
            raise FleetError(f"provider {provider_id} is not Leader-planning capable")
        if role == "worker" and not status.execution_capable:
            raise FleetError(f"provider {provider_id} is not Worker-execution capable")
        result = self.fetch_catalog(provider_id, refresh=refresh)
        if result.status != ModelCatalogStatus.OK or not result.verified_available:
            raise FleetError(
                f"no live catalog for {provider_id}: "
                + (result.diagnostics[0] if result.diagnostics else "unavailable")
            )
        model = result.model(model_id)
        if model is None or model.hidden:
            raise FleetError(
                f"model {model_id!r} is not in the current {provider_id} catalog; reselect it"
            )
        if reasoning_effort is not None and not model.supports_effort(reasoning_effort):
            raise FleetError(
                f"model {model_id!r} does not expose reasoning effort {reasoning_effort!r}"
            )
        return Selection(
            provider=provider_id,
            model=model_id,
            reasoning_effort=reasoning_effort,
            catalog_source=result.source.value,
            catalog_discovered_at=result.discovered_at,
        )

    def select_leader(
        self, provider_id: str, model_id: str, reasoning_effort: str | None = None
    ) -> Selection:
        selection = self._validate_model(
            provider_id, model_id, reasoning_effort, role="leader", refresh=False
        )
        self.leader = selection
        return selection

    def add_worker(
        self, provider_id: str, model_id: str, reasoning_effort: str | None = None
    ) -> Selection:
        selection = self._validate_model(
            provider_id, model_id, reasoning_effort, role="worker", refresh=False
        )
        self.workers.append(selection)
        return selection

    def remove_worker(self, index: int) -> None:
        if 0 <= index < len(self.workers):
            self.workers.pop(index)

    def set_max_workers(self, value: int) -> None:
        if value <= 0:
            raise FleetError("maximum parallel Workers must be positive")
        self.max_parallel_workers = value

    def selection_available(self, selection: Selection) -> bool | None:
        """True/False from a live catalog; None when no catalog has been fetched."""
        result = self.catalogs.get(selection.provider)
        if result is None or result.status != ModelCatalogStatus.OK:
            return None
        model = result.model(selection.model)
        return bool(model is not None and not model.hidden)

    def can_start_new_execution(self) -> tuple[bool, str]:
        if self.repository is None:
            return False, "planning needs a target Git repository"
        if self.leader is None:
            return False, "choose a Leader from a live catalog"
        if not self.workers:
            return False, "choose at least one Worker from a live catalog"
        for selection in (self.leader, *self.workers):
            available = self.selection_available(selection)
            if available is False:
                return (
                    False,
                    f"selected model {selection.provider}/{selection.model} is missing from a "
                    "fresh catalog; reselect before starting",
                )
        return True, ""

    # -- preferences -------------------------------------------------------

    def save_preferences(self) -> Path:
        if self.leader is None:
            raise FleetError("choose a Leader before saving")
        if not self.workers:
            raise FleetError("choose at least one Worker before saving")
        preferences = UserPreferences(
            schema_version="0.1",
            provider_executables=dict(self.provider_overrides),
            leader=self.leader,
            workers=tuple(self.workers),
            max_parallel_workers=self.max_parallel_workers,
            ui=UiPreferences(),
        )
        return preferences_module.save_preferences(preferences, self.preferences_path)

    # -- planning conversation --------------------------------------------

    def _append(self, role: str, text: str) -> None:
        self.transcript.append(TranscriptEntry(role=role, text=text))

    def _set_state(self, state: str) -> None:
        self.state = state

    def start_request(self, request: str) -> None:
        ready, reason = self.can_start_new_execution()
        if not ready:
            raise FleetError(reason)
        # Refresh catalog availability for the exact selections before a paid call.
        for selection in (self.leader, *self.workers):
            self.fetch_catalog(selection.provider, refresh=True)
            if self.selection_available(selection) is False:
                self._set_state(
                    "PLAN_READY" if self.session and self.session.plan_draft else "SETUP"
                )
                raise FleetError(
                    f"selected model {selection.provider}/{selection.model} disappeared from the "
                    "fresh catalog; reselect it"
                )
        assert self.leader is not None and self.repository is not None
        self.approved = None
        self.monitor = None
        self._cancel_requested.clear()
        self._task_cancel_events.clear()
        self.transcript = []
        self._append("user", request)
        self.session = planning.new_session(
            leader=self.leader,
            workers=tuple(self.workers),
            max_parallel_workers=self.max_parallel_workers,
            repository=self.repository,
            request=request,
        )
        self._set_state("PLANNING")
        self._run_turn()

    def answer(self, text: str) -> None:
        if self.session is None or not self.session.pending_questions:
            raise FleetError("there is no pending Leader question to answer")
        question = self.session.pending_questions[0]
        self.session.dialogue.append((question, text))
        self.session.pending_questions = self.session.pending_questions[1:]
        self._append("user", text)
        if not self.session.pending_questions:
            self._run_turn()

    def _run_turn(self) -> None:
        assert self.session is not None and self.leader is not None
        self._set_state("PLANNING")
        try:
            if self.leader_runner is not None:
                response = self.leader_runner(self.session)
            else:
                response = planning.invoke_leader_sync(self.session, self._leader_adapter())
            planning.apply_response(self.session, response)
        except planning.PlanningError as error:
            self._append("system", f"Leader planning failed: {error}.")
            self._set_state("PLAN_READY" if self.session.plan_draft else "PLANNING")
            raise FleetError(str(error)) from error
        if self.session.pending_questions:
            self._append("leader", self.session.last_message or "")
            for question in self.session.pending_questions:
                self._append("leader", question)
            self._set_state("NEEDS_ANSWER")
        else:
            self._append("leader", self.session.last_message or "")
            self._set_state("PLAN_READY")

    def _leader_adapter(self):
        assert self.session is not None and self.leader is not None
        provider = self.leader.provider
        adapter_class = LEADER_ADAPTERS.get(provider)
        if adapter_class is None:
            raise planning.PlanningError(
                "leader_not_capable", f"{provider} has no Leader planning adapter"
            )
        configured = self.settings.provider_executables.get(provider, provider)
        executable = resolve_executable(configured, base=self.session.repository)
        if executable is None:
            raise planning.PlanningError(
                "leader_unavailable", f"executable for {provider} was not found"
            )
        return adapter_class(executable)

    @property
    def plan(self):
        return self.session.plan_draft if self.session else None

    # -- approval and execution -------------------------------------------

    def approve(self, actor: str) -> handoff_module.ApprovedPlan:
        if self.plan is None:
            raise FleetError("no validated plan draft to approve")
        if self.approved is not None:
            return self.approved
        self.approved = handoff_module.approve_plan(self.plan, self.repository, actor)
        self.monitor = FleetMonitor(str(self.repository), self.plan)
        self.monitor.attach_run(self.approved.run_id)
        self.monitor.mark_approved()
        self.monitor.add_event(f"run {self.approved.run_id}: WAITING_FOR_APPROVAL")
        self._set_state("AWAITING_START")
        return self.approved

    def start_fleet(self) -> None:
        if self.approved is None or self.monitor is None:
            raise FleetError("approve the exact plan before starting the fleet")
        if self._run_thread is not None and self._run_thread.is_alive():
            raise FleetError("the fleet is already running")
        self._cancel_requested.clear()
        self._set_state("RUNNING")
        self._run_thread = threading.Thread(
            target=self._run_fleet, name="patchfleet-fleet", daemon=True
        )
        self._run_thread.start()

    def _run_fleet(self) -> None:
        assert self.approved is not None and self.monitor is not None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        cancel = asyncio.Event()

        async def watch_cancel() -> None:
            while not self._cancel_requested.is_set():
                await asyncio.sleep(0.05)
            cancel.set()

        watcher = loop.create_task(watch_cancel())
        final = "FAILED"
        try:
            final = loop.run_until_complete(
                handoff_module.start_fleet_async(
                    self.approved,
                    self.monitor,
                    cancel_event=cancel,
                    task_cancel_events=self._task_cancel_events,
                )
            )
        except Exception as error:  # pragma: no cover - defensive UI path
            self.monitor.add_event(f"fleet error: {error}")
            final = "FAILED"
        finally:
            watcher.cancel()
            loop.run_until_complete(asyncio.gather(watcher, return_exceptions=True))
            loop.close()
        if final == "VERIFYING":
            self._set_state("FINISHED")
        elif final == "CANCELLED":
            self._set_state("CANCELLED")
        else:
            self._set_state("FAILED")

    def cancel_run(self) -> None:
        self._cancel_requested.set()

    def cancel_task(self, task_id: str) -> bool:
        if self._run_thread is None or not self._run_thread.is_alive():
            return False
        event = self._task_cancel_events.setdefault(task_id, threading.Event())
        event.set()
        return True

    def snapshot(self) -> FleetSnapshot | None:
        if self.monitor is None:
            return None
        return self.monitor.snapshot()

    def reset(self) -> None:
        self.session = None
        self.approved = None
        self.monitor = None
        self.transcript = []
        self._cancel_requested.clear()
        self._task_cancel_events.clear()
        self._set_state("SETUP")
