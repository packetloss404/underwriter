"""The long-running process: one container, two jobs.

The dashboard and the agent share a SQLite journal, and a hosting volume binds
to exactly one service, so they run together in a single process rather than as
two that cannot see the same file.

Three properties this has to hold, all of them about restarts:

**A restart can happen at any moment and is not our decision.** Managed hosts
perform mandatory migrations, and a crashed process is restarted by policy. So
boot is not a special case -- every start reads the journal and rebuilds what
is open before it considers doing anything, and a cycle never assumes it is the
first.

**The agent must not trade while its own view is unrecoverable.** If recovery
reports gaps -- unreconciled orders, unconfirmed fills, an undiffed snapshot
backlog -- the loop keeps observing and keeps managing exits, but opens
nothing. Standing down is cheap; trading on a half-known book is not.

**The web server must never take the agent down, and vice versa.** They run in
separate threads with a shared stop signal, and if either dies the process
exits non-zero so the host restarts the whole thing rather than leaving a
half-alive container serving a dashboard for an agent that stopped hours ago.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

EXCHANGE = ZoneInfo("America/New_York")

# US equity options regular session.
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

# Start observing before the bell so the book, the baseline and any overnight
# assignment are established before the first entry is ever considered.
PRE_OPEN_LEAD = timedelta(minutes=20)


@dataclass(frozen=True, slots=True)
class Schedule:
    """When to run, and how often.

    `idle_interval` is deliberately long. Outside the session there is nothing
    to react to, and a tight loop against a closed market is just quota spent
    on the same answer.
    """

    cycle_interval: timedelta = timedelta(minutes=5)
    idle_interval: timedelta = timedelta(minutes=15)
    session_open: time = SESSION_OPEN
    session_close: time = SESSION_CLOSE
    pre_open_lead: timedelta = PRE_OPEN_LEAD

    def exchange_now(self, moment: datetime) -> datetime:
        return moment.astimezone(EXCHANGE)

    def is_weekday(self, moment: datetime) -> bool:
        return self.exchange_now(moment).weekday() < 5

    def in_session(self, moment: datetime) -> bool:
        """Whether the regular session is open.

        Weekends are excluded; exchange holidays are NOT, because a holiday
        calendar is one more thing to maintain and get wrong. On a holiday the
        cycle runs, the broker's clock says closed, and nothing is opened --
        the cost is a few wasted reads, and the failure mode of a stale
        hard-coded calendar is worse.
        """
        if not self.is_weekday(moment):
            return False
        now_et = self.exchange_now(moment).time()
        return self.session_open <= now_et < self.session_close

    def should_run(self, moment: datetime) -> bool:
        """Whether to run a cycle now, including the pre-open observation."""
        if not self.is_weekday(moment):
            return False
        et = self.exchange_now(moment)
        lead = datetime.combine(et.date(), self.session_open, tzinfo=EXCHANGE) - self.pre_open_lead
        close = datetime.combine(et.date(), self.session_close, tzinfo=EXCHANGE)
        return lead <= et < close

    def interval(self, moment: datetime) -> timedelta:
        return self.cycle_interval if self.should_run(moment) else self.idle_interval


@dataclass(slots=True)
class Supervisor:
    """Runs the agent loop until told to stop.

    `run_cycle` is injected rather than constructed here, so the loop is
    testable with a fake and the runtime knows nothing about market data.
    """

    run_cycle: Callable[[], object]
    schedule: Schedule = field(default_factory=Schedule)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    cycles: int = 0
    failures: int = 0
    # Consecutive failures before giving up. A run of failures usually means
    # something systemic -- expired credentials, a broker outage -- and
    # hammering it every five minutes neither helps nor tells anyone.
    max_consecutive_failures: int = 12

    def _wait(self, seconds: float) -> None:
        if self.sleep is not None:
            self.sleep(seconds)
            return
        # Waiting on the event rather than sleeping means a stop signal is
        # acted on immediately instead of after the current interval.
        self.stop.wait(seconds)

    def run_forever(self) -> int:
        """Loop until stopped. Returns a process exit code."""
        consecutive = 0
        while not self.stop.is_set():
            moment = self.clock()
            if self.schedule.should_run(moment):
                try:
                    self.run_cycle()
                    self.cycles += 1
                    consecutive = 0
                except Exception:
                    self.failures += 1
                    consecutive += 1
                    # One bad cycle must never end the process: the next one
                    # may reconcile whatever went wrong, and exiting would
                    # leave open positions with nothing watching them.
                    log.exception(
                        "cycle failed (%d consecutive, %d total)", consecutive, self.failures
                    )
                    if consecutive >= self.max_consecutive_failures:
                        log.error(
                            "giving up after %d consecutive failures; exiting so the "
                            "host restarts a clean process",
                            consecutive,
                        )
                        return 1
            self._wait(self.schedule.interval(moment).total_seconds())
        return 0


def install_signal_handlers(stop: threading.Event) -> None:
    """Stop cleanly on SIGTERM and SIGINT.

    A managed host sends SIGTERM before a migration or a redeploy. Finishing
    the current cycle and exiting is much better than being killed mid-write,
    even though the journal is crash-safe -- an orderly stop leaves nothing to
    reconcile on the way back up.
    """

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received; finishing the current cycle and stopping", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle)
