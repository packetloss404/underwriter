"""The market-regime filter: a global, hard no-entry gate.

This exists because of one structural fact about the strategy. Every short put
in the book loses together. Defined risk bounds each position, but the
positions are not independent -- in a sharp selloff they move as a block, so
per-position gates are insufficient by construction. Six individually
compliant trades can be one large directional bet.

The regime filter is the answer to that. It asks a single question about the
market as a whole, and when the answer is hostile **nothing new is opened**,
regardless of how rich the premium looks. Rich premium in a falling market is
usually not mispricing; it is the market correctly repricing risk.

Two deliberate asymmetries:

- It only ever blocks entries. It never forces liquidation, because a forced
  exit into a disorderly tape is its own risk, and existing positions already
  carry defined risk.
- Missing data blocks. If we cannot see whether the regime is safe, we assume
  it is not.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

# The instrument whose behaviour defines "the market" for this filter.
BENCHMARK = "SPY"


class RegimeBlock(StrEnum):
    """Why new premium selling is blocked. Displayed verbatim."""

    BENCHMARK_HISTORY_MISSING = "benchmark_history_missing"
    BENCHMARK_BELOW_TREND = "benchmark_below_trend"
    BENCHMARK_DRAWDOWN = "benchmark_drawdown"
    VOLATILITY_EXPANDING = "volatility_expanding"
    SCHEDULED_EVENT = "scheduled_event"


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    trend_window: int = 20
    # Trailing decline over `drawdown_lookback` sessions that blocks entry.
    drawdown_lookback: int = 3
    max_drawdown_pct: float = 2.0
    # Fraction of ranked instruments showing expanding realised volatility
    # that constitutes a market-wide expansion rather than idiosyncratic noise.
    max_expanding_fraction: float = 0.4
    # Sessions we expect to hold a position. A scheduled event inside this
    # horizon blocks new entries.
    #
    # This is a HOLDING horizon, not the contract's time to expiry. Setting it
    # to the 5-day minimum DTE blocks every entry for the whole week whenever a
    # single event sits anywhere in the window -- the agent stands down
    # completely while logging a plausible-looking reason. We exit well before
    # expiry, so the real question is whether we would still be holding through
    # the event, not whether the contract outlives it.
    #
    # The default of 1 means "we would carry this overnight into the event".
    event_lookahead_days: int = 1


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """A known volatility event. Dates are exchange-local calendar dates."""

    on: date
    name: str


# Known scheduled events inside or adjacent to the judged window.
# Non-farm payrolls lands at 08:30 ET on the final morning, roughly ninety
# minutes before the submission deadline.
KNOWN_EVENTS: tuple[ScheduledEvent, ...] = (
    ScheduledEvent(date(2026, 9, 4), "Non-farm payrolls, 08:30 ET"),
)


@dataclass(frozen=True, slots=True)
class Blocked:
    reason: RegimeBlock
    detail: str


@dataclass(frozen=True, slots=True)
class RegimeVerdict:
    """Whether new premium selling is permitted, and why not if not."""

    blocks: tuple[Blocked, ...] = ()

    @property
    def may_open(self) -> bool:
        return not self.blocks

    @property
    def reasons(self) -> tuple[RegimeBlock, ...]:
        return tuple(b.reason for b in self.blocks)


def _moving_average(values: Sequence[float], window: int) -> float | None:
    if window < 1 or len(values) < window:
        return None
    return statistics.fmean(values[-window:])


def check_trend(closes: Sequence[float], policy: RegimePolicy) -> Blocked | None:
    """Block when the benchmark is below its moving average.

    Selling puts below trend is selling insurance while the building is
    already smoking.
    """
    ma = _moving_average(closes, policy.trend_window)
    if ma is None:
        return Blocked(
            RegimeBlock.BENCHMARK_HISTORY_MISSING,
            f"Need {policy.trend_window} {BENCHMARK} closes for the trend filter, "
            f"have {len(closes)}. Cannot confirm the regime, so entries are blocked.",
        )
    last = closes[-1]
    if last < ma:
        return Blocked(
            RegimeBlock.BENCHMARK_BELOW_TREND,
            f"{BENCHMARK} {last:.2f} is below its {policy.trend_window}-session average {ma:.2f}.",
        )
    return None


def check_drawdown(closes: Sequence[float], policy: RegimePolicy) -> Blocked | None:
    """Block after a sharp recent decline, even if still above trend.

    A fast drop can leave price above a slow average while the tape is already
    disorderly, so this catches what the trend filter misses.
    """
    lookback = policy.drawdown_lookback
    if len(closes) < lookback + 1:
        return Blocked(
            RegimeBlock.BENCHMARK_HISTORY_MISSING,
            f"Need {lookback + 1} {BENCHMARK} closes for the drawdown filter, have {len(closes)}.",
        )
    prior = closes[-(lookback + 1)]
    last = closes[-1]
    if prior <= 0:
        return Blocked(
            RegimeBlock.BENCHMARK_HISTORY_MISSING,
            "Benchmark reference close is non-positive; cannot compute drawdown.",
        )
    change_pct = (last - prior) / prior * 100
    if change_pct <= -policy.max_drawdown_pct:
        return Blocked(
            RegimeBlock.BENCHMARK_DRAWDOWN,
            f"{BENCHMARK} is {change_pct:.2f}% over {lookback} sessions, at or beyond "
            f"the -{policy.max_drawdown_pct}% limit.",
        )
    return None


def check_volatility_expansion(expanding: Sequence[bool], policy: RegimePolicy) -> Blocked | None:
    """Block when realised volatility is expanding across the universe.

    One instrument with expanding realised vol is idiosyncratic. Most of them
    at once is a market event, and selling premium into it means selling
    insurance as the storm arrives.
    """
    if not expanding:
        return None
    fraction = sum(expanding) / len(expanding)
    if fraction > policy.max_expanding_fraction:
        return Blocked(
            RegimeBlock.VOLATILITY_EXPANDING,
            f"{fraction:.0%} of the universe shows expanding realised volatility, "
            f"above the {policy.max_expanding_fraction:.0%} limit.",
        )
    return None


def check_scheduled_events(
    today: date,
    policy: RegimePolicy,
    events: Sequence[ScheduledEvent] = KNOWN_EVENTS,
) -> Blocked | None:
    """Block when a scheduled event falls inside the intended holding period.

    Being flat into a known event is a rule rather than a judgement call,
    because the whole premise of selling premium is that we are not being paid
    for a specific identifiable risk.

    The horizon is how long we expect to *hold*, not how long the contract
    lives. Confusing the two blocks every entry for a week over one event.
    """
    horizon_days = policy.event_lookahead_days
    upcoming = [e for e in events if 0 <= (e.on - today).days <= horizon_days]
    if not upcoming:
        return None
    soonest = min(upcoming, key=lambda e: e.on)
    days = (soonest.on - today).days
    return Blocked(
        RegimeBlock.SCHEDULED_EVENT,
        f"{soonest.name} on {soonest.on.isoformat()} is {days} session(s) away, "
        f"inside the {horizon_days}-day holding horizon.",
    )


def evaluate_regime(
    *,
    benchmark_closes: Sequence[float],
    expanding_flags: Sequence[bool] = (),
    today: date | None = None,
    policy: RegimePolicy | None = None,
    events: Sequence[ScheduledEvent] = KNOWN_EVENTS,
) -> RegimeVerdict:
    """Assemble the regime verdict.

    Every check runs; blocks accumulate rather than short-circuit, so the
    dashboard can show all the reasons the agent is standing down.
    """
    policy = policy or RegimePolicy()
    blocks: list[Blocked] = []

    for block in (
        check_trend(benchmark_closes, policy),
        check_drawdown(benchmark_closes, policy),
        check_volatility_expansion(expanding_flags, policy),
        check_scheduled_events(today, policy, events) if today else None,
    ):
        if block is not None:
            blocks.append(block)

    # A missing-history block can arrive from both price checks; report once.
    seen: set[RegimeBlock] = set()
    deduped: list[Blocked] = []
    for block in blocks:
        if block.reason not in seen:
            seen.add(block.reason)
            deduped.append(block)
    return RegimeVerdict(blocks=tuple(deduped))
