"""When to close a position, and at what price.

Five triggers, evaluated in a deliberate order of severity. All of them are
pure functions of position state, quotes and the clock -- **none of them
consults the model**. Risk management that waits on an API call is not risk
management, and an agent that cannot close a position it opened is worse than
one that never opened it.

The ordering matters because a position can satisfy several at once, and the
reason we record drives what we do about it. A position that is both past its
profit target and inside the flatten window is exiting because of the flatten
window: the target is a preference, the window is a deadline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum

from underwriter.config import RiskLimits
from underwriter.positions import CONTRACT_MULTIPLIER, OpenSpread
from underwriter.regime import RegimeBlock, RegimeVerdict

# Regime blocks that force an EXIT rather than merely a stand-down.
#
# regime.py reports conditions without deciding how to liquidate. Most blocks
# only stand down new entries because forcing an exit into a disorderly tape is
# its own risk and open positions already carry defined risk. Three conditions
# are different in kind. An inverted curve and a sharp drawdown say the market
# is repricing risk right now; a scheduled event is known in advance, so there
# is time to flatten deliberately rather than absorb the event gap by accident.
#
# The trend filter is deliberately NOT here. Being below a 20-session average
# is an ordinary condition that would churn the book on noise.
EXIT_FORCING_BLOCKS: frozenset[RegimeBlock] = frozenset(
    {
        RegimeBlock.TERM_STRUCTURE_INVERTED,
        RegimeBlock.BENCHMARK_DRAWDOWN,
        RegimeBlock.SCHEDULED_EVENT,
    }
)


class ExitReason(StrEnum):
    """Why a position is being closed. Displayed verbatim."""

    HARD_FLATTEN = "hard_flatten"
    TIME_STOP = "time_stop"
    LOSS_LIMIT = "loss_limit"
    REGIME_BREAK = "regime_break"
    PROFIT_TARGET = "profit_target"


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    """Thresholds, as fractions of the credit originally received."""

    # Buy the spread back once most of the credit is earned. Taking profit
    # early is how premium selling actually works: the last portion of the
    # credit takes the longest to earn and carries the most gamma risk, so
    # holding for it is a poor trade in time and a worse one in risk.
    profit_take_fraction: float = 0.50
    # Close when buying it back costs this multiple of what we collected.
    loss_multiple: float = 2.0
    # Flatten this many minutes before the close on the mandatory day, so a
    # limit order has room to work before the broker's own sell-out window.
    flatten_before_close_minutes: int = 45


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """The verdict for one position."""

    spread: OpenSpread
    reason: ExitReason | None
    detail: str = ""
    urgent: bool = False

    @property
    def should_exit(self) -> bool:
        return self.reason is not None


def closing_debit(spread: OpenSpread, quotes: Mapping[str, float | None]) -> float | None:
    """What it costs per spread to flatten, priced conservatively.

    Mirror of the opening calculation: we buy back the short leg and sell the
    long one, and both move against us. Returns None when either leg lacks a
    quote, which callers must treat as "cannot evaluate" rather than as zero
    cost -- a missing quote is not a free exit.
    """
    short = quotes.get(spread.short_symbol)
    long_ = quotes.get(spread.long_symbol)
    if short is None or long_ is None:
        return None
    return short - long_


def realised_if_closed(spread: OpenSpread, debit: float) -> float:
    """Dollars kept if we flatten at this debit. Position total."""
    return (spread.credit_per_spread - debit) * CONTRACT_MULTIPLIER * spread.spreads


def past_flatten_cutoff(
    spread: OpenSpread,
    *,
    today: date,
    now_et: time,
    policy: ExitPolicy,
) -> bool:
    """Whether we are inside the mandatory flatten window for this position.

    Alpaca will sell out a position within an hour of expiry if buying power is
    insufficient for an in-the-money exercise (GOTCHAS #10). That is a broker
    action at whatever price the book offers, which is exactly the uncontrolled
    outcome defined risk exists to prevent. Being early costs a few hours of
    theta; being late surrenders price discovery.
    """
    if spread.days_to_expiry(today) > 0:
        return False
    cutoff_minutes = 16 * 60 - policy.flatten_before_close_minutes
    return (now_et.hour * 60 + now_et.minute) >= cutoff_minutes


def decide_exit(
    spread: OpenSpread,
    *,
    quotes: Mapping[str, float | None],
    regime: RegimeVerdict,
    today: date,
    now_et: time,
    limits: RiskLimits,
    policy: ExitPolicy | None = None,
) -> ExitDecision:
    """Decide whether to close one position, and say why.

    Triggers are evaluated most-severe first. A position can satisfy several at
    once, and the reason recorded is the one that governs what we do: a
    position both past its profit target and inside the flatten window is
    exiting because of the window, since the target is a preference and the
    window is a deadline.
    """
    policy = policy or ExitPolicy()
    credit = spread.credit_per_spread

    # 1. Hard flatten. A deadline, not a judgement.
    if past_flatten_cutoff(spread, today=today, now_et=now_et, policy=policy):
        return ExitDecision(
            spread,
            ExitReason.HARD_FLATTEN,
            f"Expires {spread.expiry.isoformat()} and it is "
            f"{now_et.isoformat(timespec='minutes')} ET. Closing before the "
            "broker's own sell-out window rather than surrendering the price.",
            urgent=True,
        )

    # 2. Time stop. Never hold into expiry-week gamma.
    dte = spread.days_to_expiry(today)
    if dte <= limits.force_flat_days_before_expiry:
        return ExitDecision(
            spread,
            ExitReason.TIME_STOP,
            f"{dte} day(s) to expiry, at or inside the "
            f"{limits.force_flat_days_before_expiry}-day floor.",
            urgent=True,
        )

    debit = closing_debit(spread, quotes)

    # 3. Loss limit, before the profit target: a position can only be one of
    #    these, but evaluating the loss first means a mispriced quote errs
    #    toward closing rather than holding.
    if debit is not None and credit > 0 and debit >= policy.loss_multiple * credit:
        return ExitDecision(
            spread,
            ExitReason.LOSS_LIMIT,
            f"Costs {debit:.2f} to close against {credit:.2f} collected, at or "
            f"beyond {policy.loss_multiple}x. "
            f"Realised if closed now: {realised_if_closed(spread, debit):,.0f}.",
            urgent=True,
        )

    # 4. Regime break. Conditions that say the market is repricing risk now,
    #    plus a scheduled event while there is still time to flatten. Never
    #    the ordinary trend filter.
    forcing = sorted(set(regime.reasons) & EXIT_FORCING_BLOCKS)
    if forcing:
        return ExitDecision(
            spread,
            ExitReason.REGIME_BREAK,
            "Regime turned hostile in a way that hurts short premium: "
            + ", ".join(b.value for b in forcing),
            urgent=True,
        )

    # 5. Profit target. The only non-urgent exit.
    if debit is not None and credit > 0 and debit <= policy.profit_take_fraction * credit:
        kept = realised_if_closed(spread, debit)
        return ExitDecision(
            spread,
            ExitReason.PROFIT_TARGET,
            f"Costs {debit:.2f} to close against {credit:.2f} collected; "
            f"{1 - debit / credit:.0%} of the credit is earned. Keeping "
            f"{kept:,.0f}.",
        )

    if debit is None:
        return ExitDecision(
            spread,
            None,
            "Holding. Cannot price the exit -- a leg has no quote, so the "
            "profit and loss triggers cannot be evaluated this cycle.",
        )
    return ExitDecision(spread, None, f"Holding. Costs {debit:.2f} to close.")


def decide_exits(
    spreads: list[OpenSpread],
    *,
    quotes: Mapping[str, float | None],
    regime: RegimeVerdict,
    today: date,
    now_et: time,
    limits: RiskLimits,
    policy: ExitPolicy | None = None,
) -> list[ExitDecision]:
    """Decide every open position, urgent exits first.

    Ordering the results matters when the book is larger than the number of
    orders we can place in one cycle: the deadline-driven exits must go first.
    """
    decisions = [
        decide_exit(
            s,
            quotes=quotes,
            regime=regime,
            today=today,
            now_et=now_et,
            limits=limits,
            policy=policy,
        )
        for s in spreads
    ]
    return sorted(decisions, key=lambda d: (not d.urgent, not d.should_exit, d.spread.underlying))
