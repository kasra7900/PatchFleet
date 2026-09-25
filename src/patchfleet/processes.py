"""Bounded, shell-free supervision of local executables."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

MAX_STREAM_LINE = 4096
# Re-check the process/exit state at least this often so a completion that the
# event loop does not surface through a single ``asyncio.wait`` is still noticed
# promptly. Bounds worst-case added latency for a fast local process.
POLL_INTERVAL_SECONDS = 0.05
# A process that exits around the deadline must win over the timeout. After the
# deadline we wait this long for the exit to land before terminating anything.
EXIT_SETTLE_SECONDS = 0.05
# Bound post-exit pipe draining: a grandchild that inherits the pipes must not be
# able to stall supervision indefinitely after the direct child has exited.
DRAIN_GRACE_SECONDS = 2.0
OutputCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class ProcessResult:
    status: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    pid: int


async def supervise(
    argv: list[str],
    *,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    timeout: float,
    max_output_bytes: int,
    on_start: Callable[[int], None] | None = None,
    cancel_event: asyncio.Event | None = None,
    termination_grace_seconds: float = 2,
    on_output: OutputCallback | None = None,
) -> ProcessResult:
    """Run an argv vector and retain at most max_output_bytes in total.

    ``on_output``, when supplied, receives decoded ``(stream, line)`` pairs for
    live display only. It never changes what is persisted: callers must not write
    these lines to events, SQLite, or JSONL.
    """
    if not argv or timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("argv, positive timeout, and positive output limit are required")
    started = monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if on_start is not None:
        try:
            on_start(process.pid)
        except BaseException:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            raise
    retained = 0
    totals = [0, 0]
    chunks: list[list[bytes]] = [[], []]
    decoders = [
        codecs.getincrementaldecoder("utf-8")(errors="replace"),
        codecs.getincrementaldecoder("utf-8")(errors="replace"),
    ]
    line_buffers = ["", ""]

    def emit(index: int, text: str) -> None:
        if on_output is None:
            return
        name = "stdout" if index == 0 else "stderr"
        for line in text.splitlines():
            try:
                on_output(name, line[:MAX_STREAM_LINE])
            except Exception:  # a display callback must never break supervision
                return

    async def drain(stream: asyncio.StreamReader, index: int) -> None:
        nonlocal retained
        while data := await stream.read(65536):
            totals[index] += len(data)
            keep = min(len(data), max_output_bytes - retained)
            if keep:
                chunks[index].append(data[:keep])
                retained += keep
            if on_output is not None:
                decoded = decoders[index].decode(data)
                if decoded:
                    buffer = line_buffers[index] + decoded
                    *complete, buffer = buffer.split("\n")
                    emit(index, "\n".join(complete))
                    line_buffers[index] = buffer[-MAX_STREAM_LINE:]
        if on_output is not None:
            tail = decoders[index].decode(b"", final=True)
            remaining = line_buffers[index] + tail
            if remaining.strip():
                emit(index, remaining)
            line_buffers[index] = ""

    async def feed() -> None:
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    readers = [
        asyncio.create_task(drain(process.stdout, 0)),
        asyncio.create_task(drain(process.stderr, 1)),
        asyncio.create_task(feed()),
    ]

    async def terminate() -> bool:
        """Terminate the process group; return True only if a signal was sent."""
        if process.returncode is not None:
            return False
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(process.wait(), termination_grace_seconds)
        except TimeoutError:
            if process.returncode is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
        return True

    async def drain_readers() -> None:
        try:
            await asyncio.wait_for(
                asyncio.gather(*readers, return_exceptions=True),
                timeout=DRAIN_GRACE_SECONDS,
            )
        except TimeoutError:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    timed_out = False
    cancelled = False
    waiter = asyncio.create_task(process.wait())
    cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event else None
    deadline = monotonic() + timeout
    try:
        while not waiter.done():
            if cancellation is not None and cancellation.done():
                cancelled = True
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                # Give a concurrently-exiting process a brief chance to win
                # before we treat the deadline as a real timeout.
                await asyncio.wait({waiter}, timeout=EXIT_SETTLE_SECONDS)
                if not waiter.done() and process.returncode is None:
                    timed_out = True
                break
            pending = {waiter} | ({cancellation} if cancellation is not None else set())
            await asyncio.wait(
                pending,
                timeout=min(remaining, POLL_INTERVAL_SECONDS),
                return_when=asyncio.FIRST_COMPLETED,
            )
        if (timed_out or cancelled) and not waiter.done() and process.returncode is None:
            terminated = await terminate()
            if not cancelled:
                timed_out = terminated
        elif process.returncode is not None:
            # An exit observed around the deadline/cancellation always wins.
            timed_out = False
    except asyncio.CancelledError:
        cancelled = True
        await terminate()
    finally:
        if cancellation:
            cancellation.cancel()
        await waiter
        await drain_readers()
    status = (
        "cancelled"
        if cancelled
        else "timed_out"
        if timed_out
        else "succeeded"
        if process.returncode == 0
        else "failed"
    )
    return ProcessResult(
        status=status,
        exit_code=process.returncode,
        duration_seconds=monotonic() - started,
        timed_out=timed_out,
        cancelled=cancelled,
        stdout=b"".join(chunks[0]),
        stderr=b"".join(chunks[1]),
        stdout_bytes=totals[0],
        stderr_bytes=totals[1],
        output_truncated=sum(totals) > retained,
        pid=process.pid,
    )
