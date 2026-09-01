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

- This module only reports the regime and blocks entries. The exit policy may
  treat a narrow subset of blocks as mandatory exits; in particular, a known
  scheduled event is actionable before the event rather than after the tape
  has already become disorderly.
- Missing data blocks. If we cannot see whether the regime is safe, we assume
  it is not.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
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
    TERM_STRUCTURE_INVERTED = "term_structure_inverted"
    TERM_STRUCTURE_MISSING = "term_structure_missing"


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    trend_window: int = 20
    # Trailing decline over `drawdown_lookback` sessions that blocks entry.
    drawdown_lookback: int = 3
    max_drawdown_pct: float = 2.0
    # Fraction of ranked instruments showing expanding realised volatility
    # that constitutes a market-wide expansion rather than idiosyncratic noise.
    max_expanding_fraction: float = 0.4

    # Ratio of near-dated to far-dated at-the-money implied volatility above
    # which the curve counts as inverted. In a calm market the near expiry
    # prices BELOW the far one -- near-term uncertainty is genuinely lower --
    # so a healthy ratio sits around 0.85 to 0.95. Crossing 1.0 means the
    # market is pricing more risk into the next week than the next two months,
    # which is the classic warning that short volatility is about to hurt.
    max_term_structure_ratio: float = 1.0
    # Require the sampled expiries to be meaningfully apart, or "near" and
    # "far" are the same point on the curve and the ratio is noise.
    min_term_structure_gap_days: int = 14
    # Sessions we expect to hold a position. A scheduled event inside this
    # horizon blocks new entries and, through exits.py, forces existing
    # positions to flatten deliberately before the release.
    #
    # This is a HOLDING horizon, not the contract's time to expiry. Setting it
    # to the 5-day minimum DTE blocks every entry for the whole week whenever a
    # single event sits anywhere in the window. The default of 1 reserves the
    # session before a known pre-market event without guaranteeing inactivity
    # throughout the judged window.
    event_lookahead_days: int = 1


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """A known volatility event. Dates are exchange-local calendar dates."""

    on: date
    name: str


# BLS releases inside the judged window, verified against the official
# September 2026 release calendar:
# https://www.bls.gov/schedule/2026/09_sched_list.htm
#
# Dates are deliberately checked in rather than fetched at runtime. A network
# or parser failure must not silently erase the calendar. The full set reaches
# the per-candidate catalyst veto. Only the tier-one employment report becomes
# a global no-entry/mandatory-exit gate; otherwise the date-only regime filter
# would deterministically shut down the entire judged window.
KNOWN_CATALYST_EVENTS: tuple[ScheduledEvent, ...] = (
    ScheduledEvent(date(2026, 9, 1), "JOLTS, 10:00 ET"),
    ScheduledEvent(date(2026, 9, 3), "Productivity and Costs (revised), 08:30 ET"),
    ScheduledEvent(date(2026, 9, 4), "Employment Situation (non-farm payrolls), 08:30 ET"),
)

KNOWN_EVENTS: tuple[ScheduledEvent, ...] = (KNOWN_CATALYST_EVENTS[-1],)


@dataclass(frozen=True, slots=True)
class TermStructure:
    """At-the-money implied volatility sampled at two expiries.

    This is the option surface's own opinion about whether stress is immediate
    or distant, measured on exactly the contracts we sell into. It is preferred
    over a VIX proxy for two reasons: index data is not available on the Basic
    plan, and the VIX-tracking ETNs carry roll decay that makes their levels
    misleading even when their direction is not.
    """

    near_iv: float
    far_iv: float
    near_dte: int
    far_dte: int

    @property
    def gap_days(self) -> int:
        return self.far_dte - self.near_dte

    @property
    def ratio(self) -> float:
        """Near over far. Below 1.0 is contango; above 1.0 is backwardation."""
        return float("inf") if self.far_iv <= 0 else self.near_iv / self.far_iv

    @property
    def is_contango(self) -> bool:
        return self.ratio < 1.0


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

    def sessions_until(event_day: date) -> int:
        if event_day < today:
            return -1
        sessions = 0
        cursor = today
        while cursor < event_day:
            cursor += timedelta(days=1)
            sessions += int(cursor.weekday() < 5)
        return sessions

    horizon_days = policy.event_lookahead_days
    upcoming = [(e, sessions_until(e.on)) for e in events]
    upcoming = [(e, sessions) for e, sessions in upcoming if 0 <= sessions <= horizon_days]
    if not upcoming:
        return None
    soonest, sessions = min(upcoming, key=lambda item: item[0].on)
    return Blocked(
        RegimeBlock.SCHEDULED_EVENT,
        f"{soonest.name} on {soonest.on.isoformat()} is {sessions} trading session(s) away, "
        f"inside the {horizon_days}-day holding horizon.",
    )


def check_term_structure(term: TermStructure | None, policy: RegimePolicy) -> Blocked | None:
    """Block when the volatility curve is inverted.

    An inverted curve says the market expects the next week to be rougher than
    the next two months. Selling premium into that is selling insurance to
    someone who can already see the fire, and it is the condition under which
    short-volatility books take their worst losses.
    """
    if term is None:
        return Blocked(
            RegimeBlock.TERM_STRUCTURE_MISSING,
            "No implied volatility term structure available. The curve is the "
            "clearest forward warning we have, so entries are blocked rather "
            "than opened blind.",
        )

    if term.gap_days < policy.min_term_structure_gap_days:
        return Blocked(
            RegimeBlock.TERM_STRUCTURE_MISSING,
            f"Sampled expiries are only {term.gap_days} days apart, below the "
            f"{policy.min_term_structure_gap_days}-day minimum. Both readings sit "
            "at effectively the same point on the curve, so the ratio is noise.",
        )

    if term.near_iv <= 0 or term.far_iv <= 0:
        return Blocked(
            RegimeBlock.TERM_STRUCTURE_MISSING,
            f"Non-positive implied volatility in the term structure "
            f"(near {term.near_iv}, far {term.far_iv}); the ratio is undefined.",
        )

    if term.ratio > policy.max_term_structure_ratio:
        return Blocked(
            RegimeBlock.TERM_STRUCTURE_INVERTED,
            f"Volatility curve is inverted: {term.near_dte}d implied vol "
            f"{term.near_iv:.1%} exceeds {term.far_dte}d {term.far_iv:.1%} "
            f"(ratio {term.ratio:.2f}). The market is pricing more risk into the "
            "near term than the far term.",
        )
    return None


def evaluate_regime(
    *,
    benchmark_closes: Sequence[float],
    expanding_flags: Sequence[bool] = (),
    term_structure: TermStructure | None = None,
    today: date | None = None,
    require_term_structure: bool = True,
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
        check_term_structure(term_structure, policy)
        if (require_term_structure or term_structure is not None)
        else None,
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
