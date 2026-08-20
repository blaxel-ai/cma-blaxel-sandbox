#!/usr/bin/env python3
"""Run one claimed Claude Managed Agents work item inside this sandbox."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")


def duration_seconds(value: str) -> float | None:
    """Parse ANT_MAX_IDLE; zero disables idle shutdown."""
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise ValueError("ANT_MAX_IDLE must be a non-negative number followed by ms, s, m, or h")
    amount = float(match.group(1))
    multiplier = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    seconds = amount * multiplier
    return seconds or None


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async with AsyncAnthropic(auth_token=os.environ["ANTHROPIC_ENVIRONMENT_KEY"]) as client:
        worker = EnvironmentWorker(
            client,
            workdir="/workspace",
            max_idle=duration_seconds(os.environ.get("ANT_MAX_IDLE", "60s")),
        )
        task = asyncio.create_task(worker.handle_item())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    asyncio.run(main())
