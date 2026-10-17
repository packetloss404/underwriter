"""A close report is complete only after both artifacts and its durable marker."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from underwriter import endofday
from underwriter.endofday import DailyCloseJob, read_status
from underwriter.runtime import EXCHANGE


def et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EXCHANGE)


DUE = et(2026, 10, 19, 16, 20)


def payload(session: date, observed: datetime) -> Mapping[str, object]:
    return {
        "history": {"range": {"start": session.isoformat(), "end": session.isoformat()}},
        "reports": {"test": "report"},
        "observed_at": observed.isoformat(),
    }


def test_runs_once_per_weekday_and_survives_a_restart(tmp_path: Path) -> None:
    calls: list[date] = []

    def run(session: date, observed: datetime) -> Mapping[str, object]:
        calls.append(session)
        return payload(session, observed)

    job = DailyCloseJob(tmp_path, run, clock=lambda: DUE)
    assert not job.tick(DUE - timedelta(seconds=1))
    assert job.seconds_until_due(DUE - timedelta(seconds=1)) == 1
    assert job.tick(DUE)
    assert not job.tick(DUE + timedelta(minutes=30))
    restarted = DailyCloseJob(tmp_path, run, clock=lambda: DUE)
    assert not restarted.tick(DUE + timedelta(hours=3))
    assert calls == [DUE.date()]
    status = read_status(tmp_path, DUE)
    assert status["status"] == "complete"
    assert status["completed_session_date"] == "2026-10-19"
    assert status["completed_at"] == DUE.astimezone(UTC).isoformat()
    assert status["observed_at"] == DUE.astimezone(UTC).isoformat()
    assert status["next_due"] == et(2026, 10, 20, 16, 20).astimezone(UTC).isoformat()
    assert json.loads((tmp_path / "2026-10-19" / "reports.json").read_text()) == {"test": "report"}


def test_same_day_late_start_catches_up_but_never_relabels_yesterday(tmp_path: Path) -> None:
    calls: list[date] = []

    def run(session: date, observed: datetime) -> Mapping[str, object]:
        calls.append(session)
        return payload(session, observed)

    late = et(2026, 10, 19, 23, 55)
    job = DailyCloseJob(tmp_path, run, clock=lambda: late)
    assert job.tick(late)
    tomorrow = et(2026, 10, 20, 8, 0)
    assert not job.tick(tomorrow)
    assert not (tmp_path / "2026-10-20").exists()
    assert calls == [date(2026, 10, 19)]
    fresh = DailyCloseJob(tmp_path / "fresh", run, clock=lambda: tomorrow)
    assert not fresh.tick(tomorrow)
    assert fresh.tick(tomorrow.replace(hour=17))
    assert calls[-1] == date(2026, 10, 20)


def test_weekends_and_dst_use_exchange_time_and_elapsed_deadlines(tmp_path: Path) -> None:
    friday = et(2026, 10, 30, 16, 20)
    job = DailyCloseJob(tmp_path, payload, clock=lambda: friday)
    assert job.tick(friday)
    saturday = et(2026, 10, 31, 16, 20)
    assert not job.tick(saturday)
    monday = et(2026, 11, 2, 16, 20)
    assert read_status(tmp_path, friday)["next_due"] == monday.astimezone(UTC).isoformat()
    assert job.seconds_until_due(friday) == 73 * 60 * 60  # Fall-back weekend.
    assert job.seconds_until_due(monday - timedelta(seconds=3)) == 3
    assert job.tick(monday.astimezone(UTC))


def test_failure_is_safe_and_retries_after_fifteen_minutes_across_restart(tmp_path: Path) -> None:
    attempts = 0

    def run(session: date, observed: datetime) -> Mapping[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private broker URL and sk-live-secret must not escape")
        return payload(session, observed)

    job = DailyCloseJob(tmp_path, run, clock=lambda: DUE)
    assert job.tick(DUE)
    state = read_status(tmp_path, DUE)
    assert state["status"] == "failed"
    assert state["error_type"] == "RuntimeError"
    assert state["completed_session_date"] is None
    assert "private" not in json.dumps(state)
    assert "sk-live" not in (tmp_path / "status.json").read_text()
    retry = DUE + timedelta(minutes=15)
    restarted = DailyCloseJob(tmp_path, run, clock=lambda: retry)
    assert not restarted.tick(retry - timedelta(seconds=1))
    assert restarted.seconds_until_due(retry - timedelta(seconds=1)) == 1
    assert restarted.tick(retry)
    assert attempts == 2
    assert read_status(tmp_path, retry)["status"] == "complete"


def test_a_failed_friday_retry_never_runs_as_saturdays_session(tmp_path: Path) -> None:
    late = et(2026, 10, 23, 23, 55)

    def broken(_session: date, _now: datetime) -> Mapping[str, object]:
        raise TimeoutError("broker request timed out")

    job = DailyCloseJob(tmp_path, broken)
    assert job.tick(late)
    saturday = late + timedelta(minutes=15)
    assert not job.tick(saturday)
    assert read_status(tmp_path, saturday)["next_due"] == (
        et(2026, 10, 26, 16, 20).astimezone(UTC).isoformat()
    )


def test_slow_failure_starts_retry_interval_when_the_attempt_finishes(tmp_path: Path) -> None:
    moment = DUE

    def slow_failure(_session: date, _now: datetime) -> Mapping[str, object]:
        nonlocal moment
        moment += timedelta(minutes=20)
        raise TimeoutError("slow source failed")

    job = DailyCloseJob(tmp_path, slow_failure, clock=lambda: moment)
    assert job.tick(DUE)
    retry = moment + timedelta(minutes=15)
    status = read_status(tmp_path, moment)
    assert status["retry_at"] == retry.astimezone(UTC).isoformat()
    assert status["next_due"] == retry.astimezone(UTC).isoformat()
    assert job.seconds_until_due(moment) == 15 * 60
    assert not job.tick(moment)
    assert not job.tick(retry - timedelta(seconds=1))


def test_partial_artifact_writes_are_not_completed_and_retry_repairs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = endofday._atomic_json

    def partial(path: Path, body: Mapping[str, object]) -> None:
        if path.name == "reports.json":
            raise OSError("private disk path")
        original(path, body)

    monkeypatch.setattr(endofday, "_atomic_json", partial)
    job = DailyCloseJob(tmp_path, payload, clock=lambda: DUE)
    assert job.tick(DUE)
    assert (tmp_path / "2026-10-19" / "history.json").is_file()
    assert not (tmp_path / "2026-10-19" / "status.json").exists()
    assert read_status(tmp_path, DUE)["completed_session_date"] is None
    monkeypatch.setattr(endofday, "_atomic_json", original)
    retry = DUE + timedelta(minutes=15)
    assert DailyCloseJob(tmp_path, payload, clock=lambda: retry).tick(retry)
    assert read_status(tmp_path, retry)["status"] == "complete"
    assert not list(tmp_path.rglob(".pending-*"))


def test_completed_marker_recovers_after_global_pointer_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = endofday._atomic_json

    def missing_pointer(path: Path, body: Mapping[str, object]) -> None:
        if path == tmp_path / "status.json" and body["status"] == "complete":
            raise OSError("failed after commit")
        original(path, body)

    moment = DUE

    def run(session: date, observed: datetime) -> Mapping[str, object]:
        if session != DUE.date():
            raise TimeoutError("next session failed")
        return payload(session, observed)

    monkeypatch.setattr(endofday, "_atomic_json", missing_pointer)
    job = DailyCloseJob(tmp_path, run, clock=lambda: moment)
    assert job.tick(DUE)
    assert read_status(tmp_path, DUE)["status"] == "complete"
    assert not DailyCloseJob(tmp_path, payload).tick(DUE + timedelta(minutes=30))
    tomorrow = read_status(tmp_path, DUE + timedelta(days=1))
    assert tomorrow["status"] == "complete"
    assert tomorrow["completed_session_date"] == "2026-10-19"
    assert tomorrow["completed_at"] == DUE.astimezone(UTC).isoformat()
    # The still-running scheduler must prefer that committed marker to its
    # stale in-memory failed pointer when carrying the last success forward.
    moment = DUE + timedelta(days=1)
    assert job.tick(moment)
    failed_tomorrow = read_status(tmp_path, moment)
    assert failed_tomorrow["status"] == "failed"
    assert failed_tomorrow["session_date"] == "2026-10-20"
    assert failed_tomorrow["completed_session_date"] == "2026-10-19"
    assert failed_tomorrow["completed_at"] == DUE.astimezone(UTC).isoformat()


def test_corrupt_or_missing_artifact_invalidates_a_completed_marker(tmp_path: Path) -> None:
    assert DailyCloseJob(tmp_path, payload, clock=lambda: DUE).tick(DUE)
    (tmp_path / "2026-10-19" / "reports.json").write_text("broken json")
    assert read_status(tmp_path, DUE)["status"] == "scheduled"
    assert read_status(tmp_path, DUE)["completed_session_date"] is None
    tomorrow = read_status(tmp_path, DUE + timedelta(days=1))
    assert tomorrow["status"] == "scheduled"
    assert tomorrow["completed_session_date"] is None
    assert tomorrow["completed_at"] is None
    assert DailyCloseJob(tmp_path, payload, clock=lambda: DUE).tick(DUE)
    assert read_status(tmp_path, DUE)["status"] == "complete"


def test_a_failed_new_session_is_not_replaced_with_an_unrelated_older_success(
    tmp_path: Path,
) -> None:
    assert DailyCloseJob(tmp_path, payload, clock=lambda: DUE).tick(DUE)
    tomorrow = DUE + timedelta(days=1)

    def broken(_session: date, _now: datetime) -> Mapping[str, object]:
        raise TimeoutError("new session could not refresh")

    assert DailyCloseJob(tmp_path, broken, clock=lambda: tomorrow).tick(tomorrow)
    state = read_status(tmp_path, tomorrow)
    assert state["status"] == "failed"
    assert state["session_date"] == "2026-10-20"
    assert state["completed_session_date"] == "2026-10-19"
    assert state["completed_at"] == DUE.astimezone(UTC).isoformat()


def test_market_holiday_skip_is_durable_and_creates_no_report_artifacts(tmp_path: Path) -> None:
    def holiday(_session: date, _now: datetime) -> Mapping[str, object]:
        return {"skipped": True, "reason": "market_closed_day"}

    assert DailyCloseJob(tmp_path, holiday).tick(DUE)
    assert not DailyCloseJob(tmp_path, payload).tick(DUE + timedelta(hours=1))
    status = read_status(tmp_path, DUE)
    assert status["status"] == "skipped"
    assert status["skip_reason"] == "market_closed_day"
    assert status["completed_session_date"] is None
    assert not (tmp_path / "2026-10-19" / "history.json").exists()
    assert not (tmp_path / "2026-10-19" / "reports.json").exists()


def test_a_broken_disk_still_sets_an_in_memory_retry_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_path: Path, _body: Mapping[str, object]) -> None:
        raise PermissionError("private path")

    monkeypatch.setattr(endofday, "_atomic_json", broken)
    job = DailyCloseJob(tmp_path, payload)
    assert job.tick(DUE)
    assert job.seconds_until_due(DUE) == 15 * 60
    assert not job.tick(DUE + timedelta(minutes=1))


def test_previous_day_observations_are_never_relabelled_as_todays_report(tmp_path: Path) -> None:
    def stale(session: date, _now: datetime) -> Mapping[str, object]:
        return payload(session, DUE - timedelta(days=1))

    assert DailyCloseJob(tmp_path, stale).tick(DUE)
    assert read_status(tmp_path, DUE)["status"] == "failed"
    assert not (tmp_path / "2026-10-19" / "history.json").exists()


def test_status_is_read_only_and_ignores_malformed_or_unapproved_metadata(tmp_path: Path) -> None:
    assert read_status(tmp_path, DUE)["status"] == "scheduled"
    assert not list(tmp_path.iterdir())
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "status": {"bad": "type"},
                "error": "private",
                "path": "/secret",
                "completed_session_date": "invalid",
                "observed_at": "invalid",
                "error_type": "URL/secret",
            }
        )
    )
    status = read_status(tmp_path, DUE)
    assert status["status"] == "scheduled"
    assert status["error_type"] is None
    assert status["observed_at"] is None
    assert "private" not in json.dumps(status)
    assert "path" not in status
