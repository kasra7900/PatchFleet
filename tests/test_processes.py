"""Regression tests for bounded subprocess supervision.

These are deliberately deterministic: a short-lived process that exits before
the deadline must never be reported as timed out, with or without an output
callback. True timeout and cancellation must still be reported.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from patchfleet.processes import supervise

TOTAL_ITERATIONS = 40
PER_PROCESS_BOUND_SECONDS = 2.0
TOTAL_BOUND_SECONDS = 30.0


def _short_process(tmp_path: Path) -> Path:
    script = tmp_path / "short"
    script.write_text(
        f"""#!{sys.executable}
import sys
print("short-output")
sys.exit(0)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_single_short_process_is_prompt(tmp_path: Path) -> None:
    script = _short_process(tmp_path)

    async def run() -> object:
        return await supervise([str(script)], timeout=5, max_output_bytes=4096)

    started = time.monotonic()
    result = asyncio.run(run())
    wall_seconds = time.monotonic() - started

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.duration_seconds < PER_PROCESS_BOUND_SECONDS
    assert wall_seconds < PER_PROCESS_BOUND_SECONDS


def test_short_process_never_false_times_out_without_callback(tmp_path: Path) -> None:
    script = _short_process(tmp_path)

    async def run() -> list[object]:
        return [
            await supervise([str(script)], timeout=5, max_output_bytes=4096)
            for _ in range(TOTAL_ITERATIONS)
        ]

    started = time.monotonic()
    results = asyncio.run(run())
    elapsed = time.monotonic() - started

    assert all(result.status == "succeeded" for result in results)
    assert all(result.exit_code == 0 for result in results)
    assert all(result.timed_out is False for result in results)
    assert all(result.cancelled is False for result in results)
    assert all(b"short-output" in result.stdout for result in results)
    assert all(result.duration_seconds < PER_PROCESS_BOUND_SECONDS for result in results), (
        f"slowest={max(result.duration_seconds for result in results):.3f}s"
    )
    assert elapsed < TOTAL_BOUND_SECONDS, f"{TOTAL_ITERATIONS} runs took {elapsed:.2f}s"


def test_short_process_never_false_times_out_with_output_callback(tmp_path: Path) -> None:
    script = _short_process(tmp_path)

    async def run() -> tuple[list[object], list[tuple[str, str]]]:
        lines: list[tuple[str, str]] = []
        results = []
        for _ in range(TOTAL_ITERATIONS):
            lines.clear()
            results.append(
                await supervise(
                    [str(script)],
                    timeout=5,
                    max_output_bytes=4096,
                    on_output=lambda stream, line: lines.append((stream, line)),
                )
            )
        return results, lines

    started = time.monotonic()
    results, _lines = asyncio.run(run())
    elapsed = time.monotonic() - started

    assert all(result.status == "succeeded" for result in results)
    assert all(result.timed_out is False for result in results)
    assert all(result.exit_code == 0 for result in results)
    assert all(result.duration_seconds < PER_PROCESS_BOUND_SECONDS for result in results), (
        f"slowest={max(result.duration_seconds for result in results):.3f}s"
    )
    assert elapsed < TOTAL_BOUND_SECONDS, f"{TOTAL_ITERATIONS} runs took {elapsed:.2f}s"


def test_output_callback_receives_bounded_lines(tmp_path: Path) -> None:
    script = tmp_path / "noisy"
    script.write_text(
        f"""#!{sys.executable}
import sys
for index in range(5):
    sys.stdout.write(f"line-{{index}}\\n")
sys.stderr.write("err-line\\n")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    seen: list[tuple[str, str]] = []

    async def run() -> object:
        return await supervise(
            [str(script)],
            timeout=5,
            max_output_bytes=4096,
            on_output=lambda stream, line: seen.append((stream, line)),
        )

    result = asyncio.run(run())

    assert result.status == "succeeded"
    assert ("stdout", "line-0") in seen
    assert ("stderr", "err-line") in seen
    assert all(len(line) <= 4096 for _stream, line in seen)


def test_true_timeout_still_reported(tmp_path: Path) -> None:
    async def run() -> object:
        return await supervise(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.1,
            max_output_bytes=64,
            termination_grace_seconds=0.1,
        )

    result = asyncio.run(run())

    assert result.timed_out is True
    assert result.status == "timed_out"
    assert result.cancelled is False


def test_cancellation_still_reported(tmp_path: Path) -> None:
    async def run() -> object:
        event = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, event.set)
        return await supervise(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=5,
            max_output_bytes=64,
            cancel_event=event,
            termination_grace_seconds=0.1,
        )

    result = asyncio.run(run())

    assert result.cancelled is True
    assert result.status == "cancelled"
    assert result.timed_out is False


def test_nonzero_exit_is_failure_not_timeout(tmp_path: Path) -> None:
    async def run() -> object:
        return await supervise(
            [sys.executable, "-c", "raise SystemExit(9)"],
            timeout=5,
            max_output_bytes=64,
        )

    result = asyncio.run(run())

    assert result.status == "failed"
    assert result.exit_code == 9
    assert result.timed_out is False
