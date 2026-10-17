"""Persistent, once-per-session close reports, independent of trading cycles.

The supervisor calls the job on the journal-owning thread after 16:20 Eastern
on weekdays. The callback must use bounded I/O; it observes and reports, never
opens a trade. Two atomic report files are committed by a final session marker,
so a restart after a partial write retries rather than claiming completion.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
EXCHANGE: Final = ZoneInfo("America/New_York")
CLOSE_REPORT_TIME: Final = time(16, 20)
RETRY_INTERVAL: Final = timedelta(minutes=15)
_STATES: Final = {"scheduled", "running", "complete", "failed", "skipped"}
_TIMESTAMPS: Final = ("completed_at", "observed_at", "last_attempt_at", "retry_at")
_DATES: Final = ("session_date", "completed_session_date")
_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _moment(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(result, datetime) and result.tzinfo is not None:
            return result.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _safe_state(raw: Mapping[str, object] | None) -> dict[str, Any]:
    """Publish a strict metadata allow-list, never paths or exception messages."""
    raw = raw or {}
    state: dict[str, Any] = {"status": raw.get("status", "scheduled")}
    if not isinstance(state["status"], str) or state["status"] not in _STATES:
        state["status"] = "scheduled"
    for key in _TIMESTAMPS:
        moment = _moment(raw.get(key))
        state[key] = moment.isoformat() if moment else None
    for key in _DATES:
        value = raw.get(key)
        try:
            state[key] = date.fromisoformat(value).isoformat() if isinstance(value, str) else None
        except ValueError:
            state[key] = None
    for key in ("error_type", "skip_reason"):
        value = raw.get(key)
        state[key] = value if isinstance(value, str) and _TOKEN.fullmatch(value) else None
    return state


def _committed(directory: Path, session_date: str) -> dict[str, Any] | None:
    folder = directory / session_date
    marker = _safe_state(_load(folder / "status.json"))
    if marker["session_date"] != session_date:
        return None
    if marker["status"] == "skipped":
        return marker
    if (
        marker["status"] == "complete"
        and marker["completed_session_date"] == session_date
        and marker["completed_at"]
        and marker["observed_at"]
        and _load(folder / "history.json") is not None
        and _load(folder / "reports.json") is not None
    ):
        return marker
    return None


def _state_at(directory: Path, now: datetime) -> dict[str, Any]:
    state = _safe_state(_load(directory / "status.json"))
    today = now.astimezone(EXCHANGE).date().isoformat()
    # Recover a committed session if the process died before publishing its
    # global pointer. A global pointer alone is not proof both files exist.
    committed = _committed(directory, today)
    if committed:
        return committed
    # The global pointer can still say "failed" after the session itself
    # committed. Recover that same session on later days too, while keeping a
    # genuinely failed new session distinct from an unrelated older success.
    session = state["session_date"]
    if session is not None and session < today:
        committed = _committed(directory, session)
        if committed:
            return committed
    if state["status"] == "complete":
        state.update(
            status="scheduled", completed_session_date=None, completed_at=None, observed_at=None
        )
    return state


def _next_due(now: datetime, state: Mapping[str, object]) -> datetime:
    et = now.astimezone(EXCHANGE)
    day = et.date()
    terminal = state.get("session_date") == day.isoformat() and state.get("status") in {
        "complete",
        "skipped",
    }
    due = datetime.combine(day, CLOSE_REPORT_TIME, tzinfo=EXCHANGE)
    if day.weekday() < 5 and not terminal:
        retry = (
            _moment(state.get("retry_at")) if state.get("session_date") == day.isoformat() else None
        )
        if retry is None or retry.astimezone(EXCHANGE).date() == day:
            return max(due.astimezone(UTC), retry) if retry else due.astimezone(UTC)
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, CLOSE_REPORT_TIME, tzinfo=EXCHANGE).astimezone(UTC)


def read_status(directory: Path, now: datetime) -> dict[str, Any]:
    """Safe GET metadata. Reading it never starts a refresh or changes a file."""
    state = _state_at(Path(directory), now)
    return {
        "enabled": True,
        "timezone": "America/New_York",
        "schedule": "weekdays at 16:20",
        "next_due": _next_due(now, state).isoformat(),
        **state,
    }


def _atomic_json(path: Path, body: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".pending-", delete=False
        ) as handle:
            pending = Path(handle.name)
            json.dump(dict(body), handle, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


@dataclass(slots=True)
class DailyCloseJob:
    """A synchronous close job confined to the supervisor's journal thread."""

    directory: Path
    run_job: Callable[[date, datetime], Mapping[str, object]]
    clock: Callable[[], datetime] = _utc_now
    _memory: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def _state(self, now: datetime) -> dict[str, Any]:
        state = _state_at(self.directory, now)
        if state["status"] in {"complete", "skipped"} and state["session_date"] == (
            now.astimezone(EXCHANGE).date().isoformat()
        ):
            return state
        attempted = _moment(self._memory.get("last_attempt_at"))
        persisted = _moment(state.get("last_attempt_at"))
        if attempted is not None and (
            persisted is None
            or attempted > persisted
            or (attempted == persisted and state["status"] not in {"complete", "skipped"})
        ):
            return self._memory.copy()
        return state

    def seconds_until_due(self, now: datetime) -> float:
        return max(0.0, (_next_due(now, self._state(now)) - now.astimezone(UTC)).total_seconds())

    def _persist(self, state: Mapping[str, object], *, terminal: bool = False) -> None:
        self._memory = dict(state)
        if terminal:
            session = str(state["session_date"])
            _atomic_json(self.directory / session / "status.json", state)
        _atomic_json(self.directory / "status.json", state)

    def tick(self, now: datetime) -> bool:
        """Attempt today's due job once, or retry after fifteen minutes.

        It never backfills a previous date using a new day's broker reading.
        Exceptions are contained, including inability to persist a failed job;
        an in-memory deadline still prevents a broken disk from busy-looping.
        """
        et = now.astimezone(EXCHANGE)
        if et.weekday() >= 5 or et.time() < CLOSE_REPORT_TIME:
            return False
        previous = self._state(now)
        if _next_due(now, previous) > now.astimezone(UTC):
            return False
        session = et.date().isoformat()
        state = {
            **previous,
            "status": "running",
            "session_date": session,
            "last_attempt_at": now.astimezone(UTC).isoformat(),
            "retry_at": None,
            "error_type": None,
            "skip_reason": None,
        }
        try:
            self._persist(state)
            result = self.run_job(et.date(), now)
            if result.get("skipped") is True:
                reason = result.get("reason")
                if not isinstance(reason, str) or not _TOKEN.fullmatch(reason):
                    raise ValueError("a skipped close report must name a reason code")
                folder = self.directory / session
                for name in ("history.json", "reports.json"):
                    (folder / name).unlink(missing_ok=True)
                state.update(status="skipped", skip_reason=reason)
            else:
                observed = _moment(result.get("observed_at"))
                history, reports = result.get("history"), result.get("reports")
                if (
                    observed is None
                    or observed.astimezone(EXCHANGE).date() != et.date()
                    or not isinstance(history, Mapping)
                    or not isinstance(reports, Mapping)
                ):
                    raise ValueError("close report must contain same-session observations")
                folder = self.directory / session
                _atomic_json(folder / "history.json", history)
                _atomic_json(folder / "reports.json", reports)
                completed = self.clock().astimezone(UTC)
                state.update(
                    status="complete",
                    completed_session_date=session,
                    completed_at=completed.isoformat(),
                    observed_at=observed.isoformat(),
                )
            self._persist(state, terminal=True)
        except Exception as exc:
            failed_at = max(self.clock().astimezone(UTC), now.astimezone(UTC))
            state.update(
                status="failed",
                error_type=type(exc).__name__,
                retry_at=(failed_at + RETRY_INTERVAL).isoformat(),
                completed_session_date=previous.get("completed_session_date"),
                completed_at=previous.get("completed_at"),
                observed_at=previous.get("observed_at"),
            )
            self._memory = state.copy()
            try:
                self._persist(state)
            except Exception:
                log.warning("daily close status could not be persisted")
            log.warning("daily close report failed (%s)", type(exc).__name__)
        return True
