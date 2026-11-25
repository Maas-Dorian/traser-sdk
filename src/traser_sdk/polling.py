# SPDX-License-Identifier: Apache-2.0
"""Bounded, opt-in polling for caller-supplied external state reads.

The polling helpers only repeat the SDK's existing read-only verification call.
They never retry the original write, authenticate an external source, or run
background jobs. A timeout or attempt limit is *unconfirmed*, not a failed write.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class PollResult:
    """Outcome of bounded verification against application-supplied reads."""

    status: str  # observed_match | unconfirmed_timeout | unconfirmed_attempt_limit
    attempts: int
    elapsed_ms: int

    @property
    def matched(self) -> bool:
        """True if the supplied read returned the expected state at least once."""
        return self.status == "observed_match"


def _validate_limits(timeout_s: float, interval_s: float, max_attempts: int) -> None:
    for value, name in ((timeout_s, "timeout_s"), (interval_s, "interval_s")):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive finite number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be an integer between 1 and 50")
    if not 1 <= max_attempts <= 50:
        raise ValueError("max_attempts must be between 1 and 50 (evidence limit)")


def _result(status: str, attempts: int, started: float) -> PollResult:
    return PollResult(status=status, attempts=attempts,
                      elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))


def _poll_external(check: Callable[[], bool], *, timeout_s: float,
                   interval_s: float, max_attempts: int) -> PollResult:
    """Perform an immediate check, then bounded read-only repeats."""
    _validate_limits(timeout_s, interval_s, max_attempts)
    started = time.monotonic()
    deadline = started + timeout_s
    for attempt in range(1, max_attempts + 1):
        # Deliberately propagate reader exceptions; verify_external already
        # records their type in the failed read step, without the error message.
        if check():
            return _result("observed_match", attempt, started)
        if attempt == max_attempts:
            return _result("unconfirmed_attempt_limit", attempt, started)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _result("unconfirmed_timeout", attempt, started)
        time.sleep(min(interval_s, remaining))
        if time.monotonic() >= deadline:
            return _result("unconfirmed_timeout", attempt, started)
    raise AssertionError("unreachable")


async def _apoll_external(check: Callable[[], Awaitable[bool]], *, timeout_s: float,
                          interval_s: float, max_attempts: int) -> PollResult:
    """Async bounded polling with a cooperative, nonblocking wait."""
    _validate_limits(timeout_s, interval_s, max_attempts)
    started = time.monotonic()
    deadline = started + timeout_s
    for attempt in range(1, max_attempts + 1):
        if await check():
            return _result("observed_match", attempt, started)
        if attempt == max_attempts:
            return _result("unconfirmed_attempt_limit", attempt, started)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _result("unconfirmed_timeout", attempt, started)
        await asyncio.sleep(min(interval_s, remaining))
        if time.monotonic() >= deadline:
            return _result("unconfirmed_timeout", attempt, started)
    raise AssertionError("unreachable")
