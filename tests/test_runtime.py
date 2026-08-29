"""Runtime tests.

The loop's job is to keep running. Almost everything here is about not
stopping: a failed cycle must not end the process, because exiting would leave
open positions with nothing watching them.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

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
