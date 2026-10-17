"""Runtime tests.

The loop's job is to keep running. Almost everything here is about not
stopping: a failed cycle must not end the process, because exiting would leave
open positions with nothing watching them.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from underwriter.endofday import DailyCloseJob
from underwriter.runtime import EXCHANGE, Schedule, Supervisor


def et(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=EXCHANGE)


MONDAY_MIDDAY = et(2026, 8, 31, 11, 0)
SATURDAY = et(2026, 8, 29, 11, 0)


class TestSchedule:
    def test_midday_on_a_weekday_is_in_session(self) -> None:
        assert Schedule().in_session(MONDAY_MIDDAY)

    def test_the_weekend_is_not(self) -> None:
        assert not Schedule().in_session(SATURDAY)
        assert not Schedule().should_run(SATURDAY)

    def test_before_the_bell_is_not_in_session(self) -> None:
        assert not Schedule().in_session(et(2026, 8, 31, 9, 0))

    def test_but_the_pre_open_lead_still_runs_a_cycle(self) -> None:
        # The book, the baseline and any overnight assignment must be
        # established before the first entry is considered.
        assert Schedule().should_run(et(2026, 8, 31, 9, 15))

    def test_the_lead_does_not_start_arbitrarily_early(self) -> None:
        assert not Schedule().should_run(et(2026, 8, 31, 8, 0))

    def test_the_close_ends_it(self) -> None:
        assert Schedule().should_run(et(2026, 8, 31, 15, 59))
        assert not Schedule().should_run(et(2026, 8, 31, 16, 0))

    def test_a_utc_moment_is_converted_not_assumed(self) -> None:
        # 14:00 UTC is 10:00 ET in summer: in session. Treating the UTC hour as
        # local would read it as pre-dawn and skip the whole morning.
        assert Schedule().in_session(datetime(2026, 8, 31, 14, 0, tzinfo=UTC))

    def test_the_idle_interval_is_used_out_of_hours(self) -> None:
        s = Schedule()
        assert s.interval(SATURDAY) == s.idle_interval
        assert s.interval(MONDAY_MIDDAY) == s.cycle_interval

    def test_the_final_idle_wait_lands_on_the_pre_open_boundary(self) -> None:
        s = Schedule()
        assert s.interval(et(2026, 8, 31, 9, 0)) == timedelta(minutes=10)
        assert s.interval(et(2026, 8, 31, 9, 9)) == timedelta(minutes=1)

    def test_an_earlier_idle_wait_keeps_the_normal_cadence(self) -> None:
        s = Schedule()
        assert s.interval(et(2026, 8, 31, 8, 0)) == s.idle_interval

    def test_next_run_start_skips_the_weekend(self) -> None:
        s = Schedule()
        friday_after_close = et(2026, 8, 28, 16, 0)
        assert s.next_run_start(friday_after_close) == et(2026, 8, 31, 9, 10)

    def test_boundary_wait_uses_elapsed_time_across_dst(self) -> None:
        # New York falls back on Sunday, 1 November 2026. Polling remains at
        # fifteen minutes over the long weekend rather than inheriting a
        # one-hour wall-clock error from the offset change.
        s = Schedule()
        friday_after_close = et(2026, 10, 30, 16, 0)
        assert s.interval(friday_after_close) == s.idle_interval


class TestSupervisor:
    def _supervisor(self, run: object, **kw: object) -> Supervisor:
        return Supervisor(
            run_cycle=run,  # type: ignore[arg-type]
            clock=lambda: MONDAY_MIDDAY,
            sleep=lambda _s: None,
            **kw,  # type: ignore[arg-type]
        )

    def test_runs_cycles_while_in_session(self) -> None:
        calls = []
        stop = threading.Event()

        def run() -> None:
            calls.append(1)
            if len(calls) >= 3:
                stop.set()

        s = self._supervisor(run, stop=stop)
        assert s.run_forever() == 0
        assert s.cycles == 3

    def test_a_failing_cycle_does_not_end_the_process(self) -> None:
        # The property that matters most. Exiting on one bad cycle would leave
        # open positions unmanaged until somebody noticed.
        calls = []
        stop = threading.Event()

        def run() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("broker hiccup")
            stop.set()

        s = self._supervisor(run, stop=stop)
        assert s.run_forever() == 0
        assert s.failures == 1
        assert s.cycles == 1

    def test_a_recovered_cycle_resets_the_failure_run(self) -> None:
        calls = []
        stop = threading.Event()

        def run() -> None:
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("transient")
            if len(calls) >= 4:
                stop.set()

        s = self._supervisor(run, stop=stop, max_consecutive_failures=3)
        assert s.run_forever() == 0

    def test_a_sustained_failure_run_exits_for_a_clean_restart(self) -> None:
        # Systemic problems -- expired credentials, a broker outage -- are not
        # helped by hammering every five minutes.
        def run() -> None:
            raise RuntimeError("credentials expired")

        s = self._supervisor(run, max_consecutive_failures=3)
        assert s.run_forever() == 1
        assert s.failures == 3

    def test_nothing_runs_out_of_hours(self) -> None:
        calls = []
        stop = threading.Event()

        def run() -> None:
            calls.append(1)

        s = Supervisor(
            run_cycle=run,
            clock=lambda: SATURDAY,
            sleep=lambda _s: stop.set(),
            stop=stop,
        )
        assert s.run_forever() == 0
        assert calls == []

    def test_idle_loop_wakes_on_the_exact_pre_open_boundary(self) -> None:
        moments = iter((et(2026, 8, 31, 9, 0), et(2026, 8, 31, 9, 10)))
        waits: list[float] = []
        stop = threading.Event()

        def run() -> None:
            stop.set()

        s = Supervisor(
            run_cycle=run,
            clock=lambda: next(moments),
            sleep=waits.append,
            stop=stop,
        )
        assert s.run_forever() == 0
        assert s.cycles == 1
        assert waits[0] == 10 * 60

    def test_a_stop_signal_ends_the_loop(self) -> None:
        stop = threading.Event()
        stop.set()
        s = self._supervisor(lambda: pytest.fail("should not run"), stop=stop)
        assert s.run_forever() == 0
        assert s.cycles == 0

    def test_the_wait_is_interruptible_rather_than_a_blind_sleep(self) -> None:
        # Waiting on the event means a redeploy's SIGTERM is acted on at once
        # instead of after the current interval.
        s = Supervisor(run_cycle=lambda: None, clock=lambda: SATURDAY)
        assert s.sleep is None
        s.stop.set()
        started = datetime.now(UTC)
        assert s.run_forever() == 0
        assert datetime.now(UTC) - started < timedelta(seconds=2)

    def test_close_job_wakes_at_1620_without_starting_a_trading_cycle(self, tmp_path: Path) -> None:
        moment = et(2026, 10, 19, 16, 12)
        stop = threading.Event()
        waits: list[float] = []
        callbacks: list[int] = []

        def close_job(session: object, observed: datetime) -> dict[str, object]:
            callbacks.append(threading.get_ident())
            stop.set()
            return {"history": {"session": str(session)}, "reports": {}, "observed_at": observed}

        def sleep(seconds: float) -> None:
            nonlocal moment
            waits.append(seconds)
            moment += timedelta(seconds=seconds)

        job = DailyCloseJob(tmp_path, close_job, clock=lambda: moment)
        supervisor = Supervisor(
            run_cycle=lambda: pytest.fail("close job must never run a trading cycle"),
            clock=lambda: moment,
            sleep=sleep,
            stop=stop,
            after_close=job,
        )
        assert supervisor.run_forever() == 0
        assert waits[0] == 8 * 60
        assert callbacks == [threading.get_ident()]
        assert supervisor.cycles == 0

    def test_a_close_job_failure_does_not_increment_trading_failure_counters(
        self, tmp_path: Path
    ) -> None:
        stop = threading.Event()
        now = et(2026, 10, 19, 16, 20)

        def fail(_session: object, _observed: datetime) -> dict[str, object]:
            stop.set()
            raise TimeoutError("report source failed")

        supervisor = Supervisor(
            run_cycle=lambda: pytest.fail("outside trading window"),
            clock=lambda: now,
            sleep=lambda _seconds: None,
            stop=stop,
            after_close=DailyCloseJob(tmp_path, fail),
        )
        assert supervisor.run_forever() == 0
        assert supervisor.failures == 0
        assert supervisor.cycles == 0

    def test_a_cycle_crossing_the_close_deadline_uses_a_fresh_clock(self, tmp_path: Path) -> None:
        moment = et(2026, 10, 19, 15, 59)
        stop = threading.Event()
        observed: list[datetime] = []

        def slow_cycle() -> None:
            nonlocal moment
            moment = et(2026, 10, 19, 16, 21)

        def report(_session: object, now: datetime) -> dict[str, object]:
            observed.append(now)
            stop.set()
            return {"history": {}, "reports": {}, "observed_at": now}

        supervisor = Supervisor(
            run_cycle=slow_cycle,
            clock=lambda: moment,
            sleep=lambda _seconds: None,
            stop=stop,
            after_close=DailyCloseJob(tmp_path, report, clock=lambda: moment),
        )
        assert supervisor.run_forever() == 0
        assert observed == [et(2026, 10, 19, 16, 21)]
        assert supervisor.cycles == 1

    def test_unexpected_scheduler_exception_does_not_cause_a_zero_wait_spin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = threading.Event()
        waits: list[float] = []
        now = et(2026, 10, 19, 16, 20)

        def bad_tick(_job: DailyCloseJob, _now: datetime) -> bool:
            raise RuntimeError("unexpected scheduler defect")

        def sleep(seconds: float) -> None:
            waits.append(seconds)
            stop.set()

        monkeypatch.setattr(DailyCloseJob, "tick", bad_tick)
        supervisor = Supervisor(
            run_cycle=lambda: pytest.fail("outside session"),
            clock=lambda: now,
            sleep=sleep,
            stop=stop,
            after_close=DailyCloseJob(tmp_path, lambda _day, _now: {}),
        )
        assert supervisor.run_forever() == 0
        assert waits == [15 * 60]
        assert supervisor.failures == 0
