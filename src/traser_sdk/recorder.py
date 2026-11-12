# SPDX-License-Identifier: Apache-2.0
"""Small, opt-in recorder for Traser's existing MCP evidence contract.

No network activity, background workers, database access, or automatic agent hooks.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_MISSING = object()
_STEP_KINDS = {
    "llm", "agent", "tool", "retrieval", "evaluator", "rule", "model", "state",
    "planner", "handoff", "provenance", "validation", "custom",
}
_STATUSES = {"success", "failure", "partial", "skipped", "cancelled", "unknown"}
_RUN_STATUSES = {"success", "failure", "partial", "cancelled", "unknown"}
_EVIDENCE_KINDS = {
    "tool_result", "write_receipt", "post_write_state", "consumed_state",
    "effective_config", "job_receipt", "terminal_state", "other",
}
_SOURCE_KINDS = {"trace", "log", "database", "api", "runtime", "file", "other"}
_SECRET_KEYS = {
    "password", "passwd", "secret", "client_secret", "private_key", "api_key",
    "apikey", "access_token", "refresh_token", "authorization", "cookie",
    "set_cookie", "session_token", "x_api_key", "bearer_token",
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id(value: str | None, label: str) -> str:
    result = value if value is not None else uuid.uuid4().hex
    if not isinstance(result, str) or not result.strip() or len(result) > 256:
        raise ValueError(f"{label} must be a nonempty string of at most 256 characters")
    return result


def _time(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO 8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SECRET_KEYS


def _safe_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    """JSON-only capture; unsupported values are *omitted*, never repr()'d."""
    if depth > 30:
        raise ValueError("value nesting exceeds 30 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite floating point values cannot be captured")
        return value, False
    if isinstance(value, (list, tuple)):
        entries = [_safe_value(item, depth=depth + 1) for item in value]
        return [item for item, _ in entries], any(changed for _, changed in entries)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        redacted = False
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            if _secret_key(key):
                result[key] = "[REDACTED]"
                redacted = True
            else:
                result[key], changed = _safe_value(raw, depth=depth + 1)
                redacted |= changed
        return result, redacted
    raise ValueError(f"unsupported capture value type: {type(value).__name__}")


def _capture(value: Any, limit: int) -> tuple[Any, str | None]:
    try:
        safe, redacted = _safe_value(value)
        data = json.dumps(safe, allow_nan=False, ensure_ascii=False)
        if len(data.encode("utf-8")) > limit:
            raise ValueError("captured value exceeds max_value_bytes")
        return safe, "sensitive_fields_redacted" if redacted else None
    except (ValueError, TypeError, RecursionError) as exc:
        return _MISSING, f"value_omitted: {exc}"


def _write_private_json(path: str | Path, value: dict[str, Any], *, overwrite: bool) -> Path:
    destination = Path(path)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if len(data.encode("utf-8")) > 10 * 1024 * 1024:
        raise ValueError("export exceeds Traser's 10 MiB per-run import limit")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    flags |= getattr(os, "O_NOFOLLOW", 0)  # Do not overwrite a symlink via an export path.
    fd = os.open(destination, flags, 0o600)
    if os.name == "posix":
        os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return destination


class Traser:
    """Create explicitly instrumented runs; there is no global auto-instrumentation."""

    def __init__(self, *, max_value_bytes: int = 128 * 1024):
        if not isinstance(max_value_bytes, int) or max_value_bytes < 1:
            raise ValueError("max_value_bytes must be a positive integer")
        self.max_value_bytes = max_value_bytes

    def run(self, name: str, *, run_id: str | None = None) -> RunSession:
        return RunSession(name, run_id=run_id, max_value_bytes=self.max_value_bytes)


class RunSession:
    def __init__(self, name: str, *, run_id: str | None, max_value_bytes: int):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("run name is required")
        self.name = name
        self.id = _id(run_id, "run_id")
        self.max_value_bytes = max_value_bytes
        self.started_at = _utc()
        self.ended_at: str | None = None
        self.status = "unknown"
        self._steps: list[dict[str, Any]] = []
        self._sources: list[dict[str, Any]] = [
            {"id": "sdk-runtime", "kind": "runtime", "locator": f"traser-python-sdk/run/{self.id}", "stepIds": []}
        ]
        self._source_index: dict[tuple[str, str], str] = {}
        self._observations: list[dict[str, Any]] = []
        self._requirements: list[dict[str, Any]] = []
        self._context: dict[str, str] = {}
        self._final_output: Any = _MISSING
        self._warnings: list[str] = []
        self._lock = threading.RLock()

    def __enter__(self) -> RunSession:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        with self._lock:
            if exc is not None:
                self.status = "failure"
            elif self.status == "unknown":
                self.status = "success"  # Python control flow only, NOT external outcome.
            self.ended_at = _utc()
        return False

    def set_status(self, status: str) -> None:
        if status not in _RUN_STATUSES:
            raise ValueError("invalid run status")
        with self._lock:
            self.status = status

    def context(self, *, expected_outcome: str | None = None,
                actual_outcome: str | None = None, notes: str | None = None) -> None:
        fields = {"expectedOutcome": expected_outcome, "actualOutcome": actual_outcome, "notes": notes}
        with self._lock:
            for key, value in fields.items():
                if value is not None:
                    if not isinstance(value, str) or len(value) > 4000:
                        raise ValueError(f"{key} must be a string of at most 4000 characters")
                    self._context[key] = value

    def set_final_output(self, value: Any) -> None:
        captured, warning = _capture(value, self.max_value_bytes)
        if captured is _MISSING:
            raise ValueError(f"final output cannot be recorded: {warning}")
        with self._lock:
            self._final_output = captured
            if warning:
                self._warnings.append(f"finalOutput: {warning}")

    def operation(self, name: str, *, operation_id: str | None = None,
                  idempotency_key: str | None = None,
                  side_effect_target: str | None = None) -> Operation:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("operation name is required")
        for value, label in ((idempotency_key, "idempotency_key"),
                             (side_effect_target, "side_effect_target")):
            if value is not None:
                _id(value, label)
        return Operation(self, name, _id(operation_id, "operation_id"),
                         idempotency_key, side_effect_target)

    def _begin_step(self, name: str, kind: str, *, operation: Operation | None = None,
                    attempt: int | None = None, input_value: Any = _MISSING) -> dict[str, Any]:
        if kind not in _STEP_KINDS:
            raise ValueError("unsupported step kind")
        step: dict[str, Any] = {
            "id": uuid.uuid4().hex, "sequence": 0, "kind": kind,
            "name": name, "status": "unknown", "startedAt": _utc(),
        }
        if operation is not None:
            step["operationId"] = operation.operation_id
            if operation.idempotency_key is not None:
                step["idempotencyKey"] = operation.idempotency_key
            if operation.side_effect_target is not None:
                step["sideEffectTarget"] = operation.side_effect_target
            step["attemptId"] = uuid.uuid4().hex
        if attempt is not None:
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise ValueError("attempt must be a positive integer")
            step["attempt"] = attempt
            step["retryCount"] = attempt - 1
        if input_value is not _MISSING:
            captured, warning = _capture(input_value, self.max_value_bytes)
            if captured is not _MISSING:
                step["input"] = captured
            if warning:
                step["attributes"] = {"captureWarnings": [f"input: {warning}"]}
        with self._lock:
            if len(self._steps) >= 5000:
                raise ValueError("Traser limits a run to 5000 steps")
            step["sequence"] = len(self._steps)
            self._steps.append(step)
            self._sources[0]["stepIds"].append(step["id"])
        return step

    def _end_step(self, step: dict[str, Any], *, status: str,
                  output: Any = _MISSING, error: BaseException | None = None) -> None:
        with self._lock:
            step["status"] = status
            step["endedAt"] = _utc()
            start = datetime.fromisoformat(step["startedAt"])
            end = datetime.fromisoformat(step["endedAt"])
            step["durationMs"] = max(0, int((end - start).total_seconds() * 1000))
            if output is not _MISSING:
                captured, warning = _capture(output, self.max_value_bytes)
                if captured is not _MISSING:
                    step["output"] = captured
                if warning:
                    step.setdefault("attributes", {}).setdefault("captureWarnings", []).append(f"output: {warning}")
            if error is not None:
                # No exception messages by default; they may contain credentials.
                step["error"] = {"type": type(error).__name__}

    def record_step(self, name: str, *, kind: str = "agent", status: str = "unknown",
                    input_value: Any = _MISSING, output: Any = _MISSING) -> str:
        if status not in _STATUSES:
            raise ValueError("invalid step status")
        step = self._begin_step(name, kind, input_value=input_value)
        self._end_step(step, status=status, output=output)
        return step["id"]

    def _ensure_source(self, *, kind: str, locator: str, step_id: str | None) -> str:
        if kind not in _SOURCE_KINDS:
            raise ValueError("invalid source kind")
        if not isinstance(locator, str) or not locator.strip() or len(locator) > 1024:
            raise ValueError("locator must be a nonempty string of at most 1024 characters")
        with self._lock:
            key = kind, locator
            source_id = self._source_index.get(key)
            if source_id is None:
                if len(self._sources) >= 100:
                    raise ValueError("Traser limits evidence to 100 sources")
                source_id = uuid.uuid4().hex
                self._source_index[key] = source_id
                self._sources.append({"id": source_id, "kind": kind, "locator": locator, "stepIds": []})
            source = next(s for s in self._sources if s["id"] == source_id)
            if step_id is not None and step_id not in source["stepIds"]:
                source["stepIds"].append(step_id)
            return source_id

    def _observe(self, *, kind: str, source_kind: str, locator: str,
                 value: Any, step_id: str | None, observed_at: str | None) -> str:
        if kind not in _EVIDENCE_KINDS:
            raise ValueError("invalid observation kind")
        observed = _time(observed_at, "observed_at") or _utc()
        captured, warning = _capture(value, self.max_value_bytes)
        if captured is _MISSING:
            raise ValueError(f"observation cannot be recorded: {warning}")
        with self._lock:
            if step_id is not None and not any(s["id"] == step_id for s in self._steps):
                raise ValueError("observation step_id does not exist in this run")
            if len(self._observations) >= 100:
                raise ValueError("Traser limits evidence to 100 observations")
            source_id = self._ensure_source(kind=source_kind, locator=locator, step_id=step_id)
            observation_id = uuid.uuid4().hex
            observation = {"id": observation_id, "kind": kind, "sourceId": source_id,
                           "observedAt": observed, "value": captured}
            if step_id is not None:
                observation["stepId"] = step_id
            self._observations.append(observation)
            if warning:
                self._warnings.append(f"observation {observation_id}: {warning}")
            return observation_id

    def _require(self, *, kind: str, required_for: str, step_id: str | None,
                 consistency_deadline: str | None) -> str:
        if kind not in _EVIDENCE_KINDS:
            raise ValueError("invalid requirement kind")
        if not isinstance(required_for, str) or not required_for.strip() or len(required_for) > 1024:
            raise ValueError("required_for must be a nonempty string of at most 1024 characters")
        deadline = _time(consistency_deadline, "consistency_deadline")
        with self._lock:
            if step_id is not None and not any(s["id"] == step_id for s in self._steps):
                raise ValueError("requirement step_id does not exist in this run")
            if len(self._requirements) >= 50:
                raise ValueError("Traser limits evidence to 50 requirements")
            requirement_id = uuid.uuid4().hex
            requirement = {"id": requirement_id, "kind": kind, "requiredFor": required_for}
            if step_id is not None:
                requirement["stepId"] = step_id
            if deadline is not None:
                requirement["consistencyDeadline"] = deadline
            self._requirements.append(requirement)
            return requirement_id

    def to_run(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {
                "schemaVersion": "0.1", "id": self.id, "label": self.name,
                "status": self.status, "startedAt": self.started_at,
                "steps": json.loads(json.dumps(self._steps)),
            }
            if self.ended_at is not None:
                result["endedAt"] = self.ended_at
            if self._final_output is not _MISSING:
                result["finalOutput"] = self._final_output
            if self._warnings:
                result["metadata"] = {"captureWarnings": list(self._warnings)}
            return result

    def to_request(self) -> dict[str, Any]:
        with self._lock:
            evidence = {
                "origin": "recorded", "coverage": "partial",  # Opt-in instrumentation is never proof of full coverage.
                "sources": json.loads(json.dumps(self._sources)),
                "observations": json.loads(json.dumps(self._observations)),
                "requirements": json.loads(json.dumps(self._requirements)),
            }
            result: dict[str, Any] = {"execution": self.to_run(), "evidence": evidence}
            if self._context:
                result["context"] = dict(self._context)
            return result

    def export_run(self, path: str | Path, *, overwrite: bool = False) -> Path:
        """Canonical run JSON for browser upload or MCP tracePath fallback."""
        return _write_private_json(path, self.to_run(), overwrite=overwrite)

    def export_request(self, path: str | Path, *, overwrite: bool = False) -> Path:
        """Full MCP traser_investigate argument object (not browser-import JSON)."""
        return _write_private_json(path, self.to_request(), overwrite=overwrite)


class Operation:
    def __init__(self, run: RunSession, name: str, operation_id: str,
                 idempotency_key: str | None, side_effect_target: str | None):
        self.run = run
        self.name = name
        self.operation_id = operation_id
        self.idempotency_key = idempotency_key
        self.side_effect_target = side_effect_target
        self.last_step_id: str | None = None

    def __enter__(self) -> Operation:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        return False

    def _start(self, args: tuple[Any, ...], kwargs: dict[str, Any], attempt: int) -> dict[str, Any]:
        step = self.run._begin_step(self.name, "tool", operation=self, attempt=attempt,
                                    input_value={"args": list(args), "kwargs": kwargs})
        self.last_step_id = step["id"]
        return step

    def call(self, function: Callable[..., Any], *args: Any, attempt: int = 1, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(function):
            raise TypeError("async functions require await operation.acall(...)")
        step = self._start(args, kwargs, attempt)
        start = time.monotonic()
        try:
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("call returned an awaitable; use operation.acall instead")
        except BaseException as exc:
            self.run._end_step(step, status="failure", error=exc)
            step["durationMs"] = int((time.monotonic() - start) * 1000)
            raise
        self.run._end_step(step, status="success", output=result)
        step["durationMs"] = int((time.monotonic() - start) * 1000)
        return result

    async def acall(self, function: Callable[..., Any], *args: Any, attempt: int = 1, **kwargs: Any) -> Any:
        step = self._start(args, kwargs, attempt)
        start = time.monotonic()
        try:
            result = function(*args, **kwargs)
            if not inspect.isawaitable(result):
                raise TypeError("acall requires an awaitable result")
            result = await result
        except BaseException as exc:
            self.run._end_step(step, status="failure", error=exc)
            step["durationMs"] = int((time.monotonic() - start) * 1000)
            raise
        self.run._end_step(step, status="success", output=result)
        step["durationMs"] = int((time.monotonic() - start) * 1000)
        return result

    def observe(self, *, kind: str, value: Any, source_kind: str, locator: str,
                step_id: str | None = None, observed_at: str | None = None) -> str:
        return self.run._observe(kind=kind, source_kind=source_kind, locator=locator,
                                 value=value, step_id=step_id or self.last_step_id,
                                 observed_at=observed_at)

    def require(self, *, kind: str, required_for: str, step_id: str | None = None,
                consistency_deadline: str | None = None) -> str:
        return self.run._require(kind=kind, required_for=required_for,
                                 step_id=step_id or self.last_step_id,
                                 consistency_deadline=consistency_deadline)
