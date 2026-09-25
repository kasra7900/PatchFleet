"""Full-screen Fleet TUI built on Textual.

The UI is intentionally thin: it renders controller and monitor view models and
sends user actions back to :class:`patchfleet.fleet.FleetController`. It never
touches persistence internals or provider processes directly.
"""

from __future__ import annotations

import os
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .fleet import FleetController, FleetError
from .monitor import (
    FleetLayout,
    FleetSnapshot,
    WorkerPanel,
    page_count,
    sanitize_text,
    worker_layout,
)

DEFAULT_ACTOR = os.environ.get("USER") or "local-user"


class ConfirmScreen(ModalScreen[bool]):
    """A small blocking confirmation dialog."""

    def __init__(self, question: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(self.question, id="confirm-question")
            with Horizontal(id="confirm-actions"):
                yield Button(self.confirm_label, id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)

    def key_escape(self) -> None:
        self.dismiss(False)


class SetupScreen(Screen):
    """Provider → live catalog → model → review → save."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, controller: FleetController) -> None:
        super().__init__()
        self.controller = controller
        self.provider_id: str | None = None
        self.model_id: str | None = None
        self.effort: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Fleet setup — choose from live provider catalogs", id="setup-title")
        with Horizontal(id="setup-body"):
            with Vertical(id="setup-left"):
                yield Label("Providers", classes="section-label")
                yield OptionList(id="providers")
                yield Label("Models (live catalog)", classes="section-label")
                yield OptionList(id="models")
                yield Label("Reasoning effort (optional)", classes="section-label")
                yield OptionList(id="efforts")
            with VerticalScroll(id="setup-right"):
                yield Static(id="review")
        with Horizontal(id="setup-actions"):
            yield Button("Set as Leader", id="set-leader", variant="primary")
            yield Button("Add as Worker", id="add-worker", variant="success")
            yield Button("Refresh catalog", id="refresh")
            yield Button("Remove last Worker", id="remove-worker")
            yield Button("Save & Continue", id="save", variant="warning")
            yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        providers = self.query_one("#providers", OptionList)
        for capability in self.controller.capabilities():
            badges = ["installed" if capability.installed else "missing"]
            if capability.leader_capable:
                badges.append("leader")
            if capability.worker_capable:
                badges.append("worker")
            if capability.catalog_available:
                badges.append("catalog")
            elif capability.catalog_status.value != "ok":
                badges.append(f"catalog:{capability.catalog_status.value}")
            label = f"{capability.display_name} ({capability.provider_id}) [{'/'.join(badges)}]"
            providers.add_option(Option(label, id=capability.provider_id))
        self._refresh_review()

    @on(OptionList.OptionSelected)
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        list_id = event.option_list.id
        if list_id == "providers":
            self.provider_id = event.option_id
            self.model_id = None
            self.effort = None
            self.run_worker(self._load_models, thread=True, exclusive=True)
        elif list_id == "models":
            self._model_selected(event.option_id)
        elif list_id == "efforts":
            self.effort = event.option_id
            self._refresh_review()

    def _load_models(self) -> None:
        provider_id = self.provider_id
        if provider_id is None:
            return
        result = self.controller.fetch_catalog(provider_id, refresh=True)
        self.app.call_from_thread(self._apply_models, result)

    def _apply_models(self, result) -> None:
        models = self.query_one("#models", OptionList)
        models.clear_options()
        efforts = self.query_one("#efforts", OptionList)
        efforts.clear_options()
        for model in result.visible_models():
            suffix = ""
            if model.reasoning_efforts:
                suffix = "  [" + "/".join(model.reasoning_efforts) + "]"
            models.add_option(
                Option(f"{model.display_name} ({model.model_id}){suffix}", id=model.model_id)
            )
        if not result.visible_models():
            reason = result.diagnostics[0] if result.diagnostics else "no live models"
            self.notify(f"{self.provider_id}: {reason}", severity="warning")
        self._refresh_review()

    def _model_selected(self, model_id: str) -> None:
        self.model_id = model_id
        self.effort = None
        result = self.controller.catalogs.get(self.provider_id or "")
        efforts = self.query_one("#efforts", OptionList)
        efforts.clear_options()
        if result is not None:
            model = result.model(model_id)
            if model is not None:
                for effort in model.reasoning_efforts:
                    efforts.add_option(Option(effort, id=effort))
                if model.default_reasoning_effort in model.reasoning_efforts:
                    self.effort = model.default_reasoning_effort
        self._refresh_review()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "set-leader": self._set_leader,
            "add-worker": self._add_worker,
            "refresh": self._refresh_catalog,
            "remove-worker": self._remove_worker,
            "save": self.action_save,
            "quit": self.app.exit,
        }.get(event.button.id or "")
        if handler is not None:
            handler()

    def _require_model(self) -> tuple[str, str]:
        if self.provider_id is None or self.model_id is None:
            raise FleetError("select a provider and a model from its live catalog first")
        return self.provider_id, self.model_id

    def _set_leader(self) -> None:
        try:
            provider_id, model_id = self._require_model()
            self.controller.select_leader(provider_id, model_id, self.effort)
        except FleetError as error:
            self.notify(str(error), severity="error")
        self._refresh_review()

    def _add_worker(self) -> None:
        try:
            provider_id, model_id = self._require_model()
            self.controller.add_worker(provider_id, model_id, self.effort)
        except FleetError as error:
            self.notify(str(error), severity="error")
        self._refresh_review()

    def _refresh_catalog(self) -> None:
        if self.provider_id is not None:
            self.run_worker(self._load_models, thread=True, exclusive=True)

    def _remove_worker(self) -> None:
        self.controller.remove_worker(-1)
        self._refresh_review()

    def _refresh_review(self) -> None:
        review = self.query_one("#review", Static)
        review.update(self._review_text())

    def _review_text(self) -> str:
        controller = self.controller
        lines = ["Review before saving", ""]
        lines.append("Leader: " + self._selection(controller.leader))
        if controller.workers:
            lines.append("Workers:")
            lines.extend("  " + self._selection(worker) for worker in controller.workers)
        else:
            lines.append("Workers: none yet")
        lines.append(f"Maximum parallel Workers: {controller.max_parallel_workers}")
        lines.append("")
        lines.append("Catalog sources (session cache):")
        if controller.catalogs:
            for provider_id, result in sorted(controller.catalogs.items()):
                lines.append(
                    f"  {provider_id}: {result.source.value} @ "
                    f"{result.discovered_at.isoformat(timespec='seconds')} "
                    f"[{result.status.value}]"
                )
        else:
            lines.append("  none fetched yet")
        return "\n".join(lines)

    @staticmethod
    def _selection(selection) -> str:
        if selection is None:
            return "not selected"
        effort = f" ({selection.reasoning_effort})" if selection.reasoning_effort else ""
        source = selection.catalog_source or "unknown"
        return f"{selection.provider} / {selection.model}{effort}  [catalog: {source}]"

    def action_save(self) -> None:
        try:
            self.controller.save_preferences()
        except FleetError as error:
            self.notify(str(error), severity="error")
            return
        self.app.push_screen(PlanningScreen(self.controller))
        self.notify("Saved personal defaults", severity="information")

    def action_back(self) -> None:
        self.app.pop_screen()


class PlanningScreen(Screen):
    """Leader conversation, plan review, approval, and fleet start."""

    BINDINGS = [
        Binding("escape", "focus_composer", "Composer", show=False),
        Binding("ctrl+n", "new_request", "New", show=False),
        Binding("ctrl+s", "settings", "Settings", show=False),
        Binding("q", "request_quit", "Quit", show=False),
    ]

    def __init__(self, controller: FleetController) -> None:
        super().__init__()
        self.controller = controller
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="planning-header")
        yield RichLog(id="conversation", wrap=True, markup=False)
        yield Static(id="planning-status")
        with Horizontal(id="composer-row"):
            yield Input(placeholder="Describe what you want to build…", id="composer")
        with Horizontal(id="planning-actions"):
            yield Button("New", id="p-new")
            yield Button("Settings", id="p-settings")
            yield Button("Refresh catalog", id="p-refresh")
            yield Button("Plan", id="p-plan")
            yield Button("Approve", id="p-approve", variant="warning")
            yield Button("Start fleet", id="p-start", variant="success")
            yield Button("Cancel", id="p-cancel")
            yield Button("Quit", id="p-quit")
        yield Footer()

    def on_mount(self) -> None:
        self._render_header()
        self._render_status()
        self._render_transcript()

    def _render_header(self) -> None:
        controller = self.controller
        repo = str(controller.repository) if controller.repository else "no repository"
        workers = (
            ", ".join(f"{worker.provider}/{worker.model}" for worker in controller.workers)
            or "none"
        )
        healthy = []
        for capability in controller.capabilities():
            if capability.catalog_available:
                healthy.append(f"{capability.provider_id}:ok")
            elif capability.installed:
                healthy.append(f"{capability.provider_id}:no-catalog")
        self.query_one("#planning-header", Static).update(
            f"[b]Fleet[/b]  {repo}\n"
            f"Leader: {self._selection(controller.leader)}   "
            f"Workers: {workers}   Parallel: {controller.max_parallel_workers}\n"
            f"Providers: {', '.join(healthy) or 'none'}"
        )

    @staticmethod
    def _selection(selection) -> str:
        if selection is None:
            return "not configured"
        effort = f" ({selection.reasoning_effort})" if selection.reasoning_effort else ""
        return f"{selection.provider} / {selection.model}{effort}"

    def _render_status(self) -> None:
        self.query_one("#planning-status", Static).update(
            f"State: [b]{self.controller.state}[/b] — "
            "Planning is read-only; a plan draft is not execution approval."
        )

    def _render_transcript(self) -> None:
        log = self.query_one("#conversation", RichLog)
        log.clear()
        for entry in self.controller.transcript:
            prefix = {"user": "> ", "leader": "Leader: ", "system": "! "}.get(entry.role, "")
            cleaned, _ = sanitize_text(entry.text)
            log.write(prefix + cleaned)

    def _render_plan(self) -> None:
        plan = self.controller.plan
        log = self.query_one("#conversation", RichLog)
        if plan is None:
            self.notify("No validated plan draft yet", severity="warning")
            return
        log.write("— Plan draft —")
        for task in plan.tasks:
            dependency = f" after {', '.join(task.dependencies)}" if task.dependencies else ""
            log.write(
                f"  {task.task_id}: {task.title}{dependency}  [{task.assigned_provider}/{task.selected_model}]"
            )
        log.write("Not approved. Approve to create the exact durable run.")

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._busy:
            return
        self.query_one("#composer", Input).value = ""
        if self.controller.state == "NEEDS_ANSWER":
            self._run_threaded(self.controller.answer, text)
        else:
            self._run_threaded(self.controller.start_request, text)

    def _run_threaded(self, function, *args) -> None:
        self._busy = True

        def task() -> None:
            error: str | None = None
            try:
                function(*args)
            except (FleetError, ValueError) as exc:
                error = str(exc)
            self.app.call_from_thread(self._after_turn, error)

        self.run_worker(task, thread=True, exclusive=True)

    def _after_turn(self, error: str | None) -> None:
        self._busy = False
        if error:
            self.notify(error, severity="error")
        self._render_header()
        self._render_status()
        self._render_transcript()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "p-new": self.action_new_request,
            "p-settings": self.action_settings,
            "p-refresh": self.action_refresh,
            "p-plan": self._render_plan,
            "p-approve": self.action_approve,
            "p-start": self.action_start,
            "p-cancel": self.action_new_request,
            "p-quit": self.action_request_quit,
        }.get(event.button.id or "")
        if handler is not None:
            handler()

    def action_new_request(self) -> None:
        self.controller.reset()
        self.query_one("#conversation", RichLog).clear()
        self._render_header()
        self._render_status()
        self.notify("Started a new fleet session")

    def action_settings(self) -> None:
        self.app.push_screen(SetupScreen(self.controller))

    def action_refresh(self) -> None:
        for capability in self.controller.capabilities():
            self.controller.fetch_catalog(capability.provider_id, refresh=True)
        self._render_header()
        self.notify("Catalogs refreshed")

    def action_approve(self) -> None:
        try:
            approved = self.controller.approve(DEFAULT_ACTOR)
        except FleetError as error:
            self.notify(str(error), severity="error")
            return
        self._render_status()
        self.notify(f"Run {approved.run_id} approved for the exact plan", severity="information")

    def action_start(self) -> None:
        try:
            self.controller.start_fleet()
        except FleetError as error:
            self.notify(str(error), severity="error")
            return
        self.app.push_screen(FleetScreen(self.controller))
        self.notify("Fleet started", severity="information")

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    def action_request_quit(self) -> None:
        self.app.exit()


class FleetScreen(Screen):
    """Leader status panel plus live Worker panels."""

    BINDINGS = [
        Binding("q", "request_quit", "Quit", show=False),
        Binding("c", "cancel_run", "Cancel fleet", show=False),
        Binding("b", "back", "Back to planning", show=False),
        Binding("n", "next_page", "Next page", show=False),
        Binding("p", "prev_page", "Prev page", show=False),
        Binding("x", "cancel_worker", "Cancel Worker", show=False),
    ]

    def __init__(self, controller: FleetController) -> None:
        super().__init__()
        self.controller = controller
        self.page = 0
        self.fleet_layout = FleetLayout(0, 0, False, False)
        self._panel_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="leader-panel")
        yield Static(id="page-indicator")
        yield Grid(id="workers")
        with Horizontal(id="fleet-actions"):
            yield Button("Cancel fleet", id="f-cancel", variant="error")
            yield Button("Back to planning", id="f-back")
            yield Button("Quit", id="f-quit")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self.refresh_view(), exclusive=False)
        self.set_interval(0.25, self.refresh_view)

    def _terminal_size(self) -> tuple[int, int]:
        size = self.app.size
        return size.width, size.height

    async def refresh_view(self) -> None:
        snapshot = self.controller.snapshot()
        if snapshot is None:
            return
        self._render_leader(snapshot)
        width, height = self._terminal_size()
        layout = worker_layout(len(snapshot.workers), width=width, height=height)
        if layout != self.fleet_layout or not self._panel_ids:
            self.fleet_layout = layout
            await self._rebuild_workers()
        self._render_workers(snapshot, layout)
        self._render_page_indicator(snapshot, layout)

    def _render_leader(self, snapshot: FleetSnapshot) -> None:
        leader = snapshot.leader
        lines = [
            "[b]LEADER[/b]",
            f"{leader.leader}   plan {leader.plan_id}   run {leader.run_id or '—'}",
            f"Run state: {leader.run_state}   approved: {leader.approved}",
            f"Tasks: {leader.task_counts or {}}",
            f"Next: {leader.next_action}",
            leader.phase_note,
        ]
        for event in leader.recent_events[-5:]:
            cleaned, _ = sanitize_text(event)
            lines.append("  · " + cleaned)
        self.query_one("#leader-panel", Static).update("\n".join(lines))

    async def _rebuild_workers(self) -> None:
        grid = self.query_one("#workers", Grid)
        await grid.remove_children()
        self._panel_ids = []
        capacity = self.fleet_layout.capacity()
        widgets = [
            Static(id=f"worker-panel-{index}", classes="worker-panel") for index in range(capacity)
        ]
        for widget in widgets:
            self._panel_ids.append(widget.id or "")
        if widgets:
            await grid.mount(*widgets)
        if self.fleet_layout.columns:
            grid.styles.grid_size_columns = self.fleet_layout.columns

    def _render_workers(self, snapshot: FleetSnapshot, layout: FleetLayout) -> None:
        capacity = layout.capacity()
        start = self.page * capacity
        visible = snapshot.workers[start : start + capacity]
        for index, panel_id in enumerate(self._panel_ids):
            panel = self.query_one(f"#{panel_id}", Static)
            if index < len(visible):
                panel.display = True
                panel.update(self._format_worker(visible[index]))
            else:
                panel.display = False
                panel.update("")

    def _format_worker(self, worker: WorkerPanel) -> str:
        lines = [
            f"[b]{worker.task_id}[/b]  {worker.title}",
            f"{worker.provider} / {worker.model}   state: {worker.state}",
            f"attempt {worker.attempt}   elapsed {worker.elapsed_seconds:0.1f}s",
            f"worktree: {worker.worktree or '—'}",
        ]
        if worker.reason:
            lines.append(f"reason: {worker.reason}")
        if worker.dependencies:
            lines.append("after: " + ", ".join(worker.dependencies))
        lines.append("output tail:")
        tail = worker.output[-8:]
        lines.extend("  " + line for line in tail) if tail else lines.append("  (no output yet)")
        if worker.output_truncated:
            lines.append("  [output truncated]")
        return "\n".join(lines)

    def _render_page_indicator(self, snapshot: FleetSnapshot, layout: FleetLayout) -> None:
        pages = page_count(len(snapshot.workers), layout)
        mode = "compact" if layout.compact else f"{layout.columns}×{layout.rows}"
        self.page = min(self.page, pages - 1)
        self.query_one("#page-indicator", Static).update(
            f"Workers page {self.page + 1}/{pages}   layout: {mode}   "
            "keys: n/p page · x cancel worker · c cancel fleet · b planning · q quit"
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "f-cancel":
            self.action_cancel_run()
        elif event.button.id == "f-back":
            self.action_back()
        elif event.button.id == "f-quit":
            self.action_request_quit()

    def action_next_page(self) -> None:
        snapshot = self.controller.snapshot()
        if snapshot is None:
            return
        pages = page_count(len(snapshot.workers), self.fleet_layout)
        self.page = min(self.page + 1, pages - 1)

    def action_prev_page(self) -> None:
        self.page = max(self.page - 1, 0)

    def action_cancel_worker(self) -> None:
        """Request cancellation of the first cancellable Worker on this page."""
        snapshot = self.controller.snapshot()
        if snapshot is None:
            return
        start = self.page * self.fleet_layout.capacity()
        visible = snapshot.workers[start : start + self.fleet_layout.capacity()]
        candidate = next(
            (worker for worker in visible if worker.state in {"PENDING", "PROVISIONED", "RUNNING"}),
            None,
        )
        if candidate is None:
            self.notify("No cancellable Worker on this page", severity="warning")
            return
        if self.controller.cancel_task(candidate.task_id):
            self.notify(f"Cancellation requested for {candidate.task_id}")
        else:
            self.notify("The fleet is not running", severity="warning")

    def action_cancel_run(self) -> None:
        def confirmed(decision: bool | None) -> None:
            if decision:
                self.controller.cancel_run()
                self.notify("Cancellation requested; running Workers will be stopped")

        self.app.push_screen(
            ConfirmScreen("Cancel the running fleet?", confirm_label="Cancel fleet"), confirmed
        )

    def action_back(self) -> None:
        if self.controller.state == "RUNNING":

            def confirmed(decision: bool | None) -> None:
                if decision:
                    self.controller.cancel_run()
                    self.app.pop_screen()

            self.app.push_screen(
                ConfirmScreen(
                    "A fleet is still running. Cancel it before returning to planning?",
                    confirm_label="Cancel and return",
                ),
                confirmed,
            )
            return
        self.app.pop_screen()

    def action_request_quit(self) -> None:
        if self.controller.state == "RUNNING":

            def confirmed(decision: bool | None) -> None:
                if decision is None:
                    return
                if decision:
                    self.controller.cancel_run()
                    self.app.exit()
                else:
                    self.notify("Fleet continues in this session", severity="information")

            self.app.push_screen(
                ConfirmScreen(
                    "The fleet is still running. Cancel it and quit? "
                    "(No background daemon continues the run.)",
                    confirm_label="Cancel and quit",
                ),
                confirmed,
            )
            return
        self.app.exit()


class PatchFleetApp(App):
    """PatchFleet full-screen application."""

    CSS = """
    Screen { layout: vertical; }
    #setup-title { padding: 1 2; text-style: bold; }
    #setup-body { height: 1fr; }
    #setup-left { width: 55%; padding: 0 1; }
    #setup-right { width: 45%; border: round $primary; padding: 0 1; }
    .section-label { text-style: bold; margin-top: 1; }
    #providers { height: 8; }
    #models { height: 10; }
    #efforts { height: 5; }
    #setup-actions, #planning-actions, #fleet-actions { height: 3; }
    #planning-header { padding: 1 2; border: round $primary; height: auto; }
    #conversation { height: 1fr; border: round $accent; padding: 0 1; }
    #planning-status { height: 2; padding: 0 2; }
    #leader-panel { height: 35%; border: round $success; padding: 0 1; }
    #page-indicator { height: 2; padding: 0 2; }
    #workers { height: 1fr; }
    .worker-panel { border: round $secondary; padding: 0 1; }
    #confirm-box { width: 60; height: auto; border: thick $error; background: $surface; padding: 1 2; margin: 4 8; }
    #confirm-actions { height: 3; }
    """

    TITLE = "PatchFleet"
    SUB_TITLE = "Local-first, human-approved agent fleet"

    def __init__(self, controller: FleetController) -> None:
        super().__init__()
        self.controller = controller

    def on_mount(self) -> None:
        if self.controller.state == "SETUP":
            self.push_screen(SetupScreen(self.controller))
        else:
            self.push_screen(PlanningScreen(self.controller))


def run_tui(controller: FleetController) -> None:
    """Launch the full-screen application."""
    PatchFleetApp(controller).run()


def launch(repository: Path | None, *, preferences_path: Path | None = None) -> int:
    """Build the controller from discovered state and run the TUI."""
    from .discovery import discover_providers_sync
    from .settings import resolve_settings

    preferences = None
    try:
        from . import preferences as preferences_module

        preferences = preferences_module.load_preferences(preferences_path)
    except Exception:  # malformed preferences are handled by settings commands
        preferences = None
    settings = resolve_settings(repository, preferences)
    statuses = discover_providers_sync(settings.provider_executables, repository=repository)
    controller = FleetController(
        repository=repository,
        statuses=statuses,
        settings=settings,
        preferences_path=preferences_path,
    )
    if preferences is not None:
        controller.leader = preferences.leader
        controller.workers = list(preferences.workers)
        controller.max_parallel_workers = preferences.max_parallel_workers
    run_tui(controller)
    return 0


def tui_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True
