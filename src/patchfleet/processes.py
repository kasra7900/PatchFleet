"""Bounded, shell-free supervision of local executables."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic


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
) -> ProcessResult:
    """Run an argv vector and retain at most max_output_bytes in total."""
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

    async def drain(stream: asyncio.StreamReader, index: int) -> None:
        nonlocal retained
        while data := await stream.read(65536):
            totals[index] += len(data)
            keep = min(len(data), max_output_bytes - retained)
            if keep:
                chunks[index].append(data[:keep])
                retained += keep

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

    async def terminate() -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), termination_grace_seconds)
        except TimeoutError:
            if process.returncode is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()

    timed_out = False
    cancelled = False
    waiter = asyncio.create_task(process.wait())
    cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event else None
    try:
        pending = {waiter} | ({cancellation} if cancellation else set())
        done, _ = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            cancelled = cancellation in done if cancellation else False
            timed_out = not cancelled
            await terminate()
    except asyncio.CancelledError:
        cancelled = True
        await terminate()
    finally:
        if cancellation:
            cancellation.cancel()
        await waiter
        await asyncio.gather(*readers, return_exceptions=True)
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
