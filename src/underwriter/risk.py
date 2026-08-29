"""The risk engine: the last thing between a signal and an order.

Three rules govern everything here.

**It never depends on the AI.** Sizing, caps, and stops are arithmetic on
account state. If the classifier is unavailable, malformed, or wrong, risk
management still works. That is the whole reason the AI is confined to
producing a thesis.

**It denies with reasons.** Every refusal names itself so the dashboard and the
audit log can show a judge exactly which gate stopped a trade. A silent `False`
is unexplainable, and an unexplainable risk system is indistinguishable from a
broken one.

**It fails closed.** Unreadable equity, an unknown symbol, or a nonsensical
quantity denies the trade. Absence of evidence is not evidence of safety.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from enum import StrEnum

from underwriter.config import RiskLimits
from underwriter.universe import are_correlated, is_tradeable


class Denial(StrEnum):
    """Why a proposed trade was refused. Displayed verbatim."""

    KILL_SWITCH = "kill_switch"
    UNKNOWN_SYMBOL = "unknown_symbol"
    UNREADABLE_EQUITY = "unreadable_equity"
    NONPOSITIVE_RISK = "nonpositive_risk"
    POSITION_CAP = "position_cap"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    CORRELATED_EXPOSURE = "correlated_exposure"
    AGGREGATE_RISK_CAP = "aggregate_risk_cap"
    DAILY_LOSS_STOP = "daily_loss_stop"
    TOO_LATE_IN_SESSION = "too_late_in_session"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    SIZE_ROUNDS_TO_ZERO = "size_rounds_to_zero"
    AGGREGATE_DELTA_CAP = "aggregate_delta_cap"
    DELTA_UNKNOWN = "delta_unknown"
    UNREADABLE_BASELINE = "unreadable_baseline"
    UNREADABLE_PNL = "unreadable_pnl"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """An open defined-risk position, as the risk engine needs to see it."""

    symbol: str
    max_loss: float
    unrealised_pnl: float = 0.0
    # Net directional exposure in equivalent shares of the underlying. Positive
    # is long. A put credit spread contributes positive delta.
    net_delta: float = 0.0


@dataclass(frozen=True, slots=True)
class AccountState:
    """Account facts the risk engine reasons over.

    `starting_equity` is the session's opening equity, used for the daily loss
    stop. It is a separate input rather than derived, because deriving it from
    current equity mid-session would make the stop drift with P&L and never
    trigger.
    """

    equity: float
    options_buying_power: float
    # Both are Optional because the journal genuinely returns None when it
    # cannot supply them, and the caller must not paper over that with `or
    # 0.0`. A zero baseline used to skip the daily loss stop entirely: down 9%
    # on the day, the agent opened another position with no denial and nothing
    # in the audit log. Two harmless-looking fallbacks at a call site each
    # turned the stop off.
    starting_equity: float | None
    realised_pnl_today: float | None = 0.0
    open_positions: Sequence[OpenPosition] = field(default_factory=tuple)

    @property
    def open_risk(self) -> float:
        return sum(p.max_loss for p in self.open_positions)

    @property
    def net_delta(self) -> float:
        """The book's aggregate directional exposure, in share equivalents."""
        return sum(p.net_delta for p in self.open_positions)

    @property
    def unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl for p in self.open_positions)

    @property
    def conservative_day_pnl(self) -> float | None:
        """Realised plus unrealised, or None when realised P&L is unreadable.

        Unrealised losses count against the daily stop; unrealised *gains* do
        not offset it. A stop that a paper profit can unlock is not a stop.
        """
        if self.realised_pnl_today is None:
            return None
        return self.realised_pnl_today + min(0.0, self.unrealised_pnl)


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's answer. `contracts` is meaningful only when allowed."""

    allowed: bool
    contracts: int = 0
    denials: tuple[Denial, ...] = ()
    detail: tuple[str, ...] = ()

    @classmethod
    def deny(cls, reason: Denial, detail: str = "") -> Decision:
        return cls(allowed=False, denials=(reason,), detail=(detail,) if detail else ())

    @classmethod
    def deny_all(cls, reasons: Sequence[tuple[Denial, str]]) -> Decision:
        return cls(
            allowed=False,
            denials=tuple(r for r, _ in reasons),
            detail=tuple(d for _, d in reasons if d),
        )


def max_risk_dollars(equity: float, limits: RiskLimits) -> float:
    """Dollar risk budget for a single trade."""
    return equity * (limits.max_risk_per_trade_pct / 100)


def size_position(*, equity: float, max_loss_per_contract: float, limits: RiskLimits) -> int:
    """Contracts affordable within the per-trade risk budget.

    Floors rather than rounds: rounding up would breach the cap the number
    exists to enforce. Returns 0 when even one contract is too large, which the
    caller must treat as a denial rather than as a zero-size order.
    """
    if equity <= 0 or max_loss_per_contract <= 0:
        return 0
    budget = max_risk_dollars(equity, limits)
    return max(0, math.floor(budget / max_loss_per_contract))


def evaluate(
    *,
    symbol: str,
    max_loss_per_contract: float,
    account: AccountState,
    limits: RiskLimits,
    now_et: time,
    kill_switch: bool = False,
    net_delta_per_contract: float | None = None,
) -> Decision:
    """Decide whether a proposed defined-risk position may be opened.

    Every applicable gate is evaluated rather than short-circuiting on the
    first failure, so the audit log shows all the reasons a trade was refused
    rather than only the first one encountered.
    """
    reasons: list[tuple[Denial, str]] = []

    if kill_switch:
        reasons.append((Denial.KILL_SWITCH, "Kill switch engaged; no new entries."))

    if not is_tradeable(symbol):
        reasons.append((Denial.UNKNOWN_SYMBOL, f"{symbol} is not in the universe."))

    if not math.isfinite(account.equity) or account.equity <= 0:
        reasons.append((Denial.UNREADABLE_EQUITY, f"Equity unusable: {account.equity!r}."))
        # Every remaining gate is a function of equity, so stop here rather
        # than emit a cascade of derived nonsense.
        return Decision.deny_all(reasons)

    if not math.isfinite(max_loss_per_contract) or max_loss_per_contract <= 0:
        reasons.append(
            (
                Denial.NONPOSITIVE_RISK,
                f"Defined risk must be positive, got {max_loss_per_contract!r}.",
            )
        )
        return Decision.deny_all(reasons)

    # Session timing.
    cutoff = time.fromisoformat(limits.no_new_entries_after_et)
    if now_et >= cutoff:
        reasons.append(
            (
                Denial.TOO_LATE_IN_SESSION,
                f"{now_et.isoformat(timespec='minutes')} ET is past the "
                f"{limits.no_new_entries_after_et} ET entry cutoff.",
            )
        )

    # Daily loss stop, measured against the session's opening equity.
    #
    # An unreadable baseline or unreadable realised P&L DENIES. Skipping the
    # check because an input is missing turns the stop off precisely when we
    # can see least, and it does so silently -- the failure looks identical to
    # a healthy day.
    day_pnl = account.conservative_day_pnl
    if account.starting_equity is None or account.starting_equity <= 0:
        reasons.append(
            (
                Denial.UNREADABLE_BASELINE,
                f"Session-open equity is {account.starting_equity!r}, so the "
                f"{limits.daily_loss_stop_pct}% daily loss stop cannot be measured.",
            )
        )
    elif day_pnl is None:
        reasons.append(
            (
                Denial.UNREADABLE_PNL,
                "Realised P&L for the session is unreadable, so the daily loss "
                "stop cannot be evaluated.",
            )
        )
    else:
        loss_limit = account.starting_equity * (limits.daily_loss_stop_pct / 100)
        if day_pnl <= -loss_limit:
            reasons.append(
                (
                    Denial.DAILY_LOSS_STOP,
                    f"Day P&L {day_pnl:,.2f} breaches the "
                    f"{limits.daily_loss_stop_pct}% stop ({-loss_limit:,.2f}).",
                )
            )

    # Concurrency and concentration.
    held = [p.symbol for p in account.open_positions]
    if len(held) >= limits.max_concurrent_positions:
        reasons.append(
            (
                Denial.POSITION_CAP,
                f"{len(held)} open positions is at the cap of {limits.max_concurrent_positions}.",
            )
        )

    if symbol in held:
        reasons.append((Denial.DUPLICATE_SYMBOL, f"Already holding a position in {symbol}."))

    correlated = sorted({h for h in held if are_correlated(symbol, h)})
    if correlated:
        reasons.append(
            (
                Denial.CORRELATED_EXPOSURE,
                f"{symbol} overlaps existing exposure in {', '.join(correlated)}; "
                "holding both would be one bet, not two.",
            )
        )

    # Sizing, and the caps that depend on it.
    contracts = size_position(
        equity=account.equity, max_loss_per_contract=max_loss_per_contract, limits=limits
    )
    if contracts < 1:
        reasons.append(
            (
                Denial.SIZE_ROUNDS_TO_ZERO,
                f"One contract risks {max_loss_per_contract:,.2f}, above the "
                f"{limits.max_risk_per_trade_pct}% per-trade budget of "
                f"{max_risk_dollars(account.equity, limits):,.2f}.",
            )
        )
        return Decision.deny_all(reasons)

    proposed_risk = contracts * max_loss_per_contract

    aggregate_cap = account.equity * (limits.max_total_open_risk_pct / 100)
    if account.open_risk + proposed_risk > aggregate_cap:
        reasons.append(
            (
                Denial.AGGREGATE_RISK_CAP,
                f"Open risk {account.open_risk:,.2f} plus proposed "
                f"{proposed_risk:,.2f} exceeds the "
                f"{limits.max_total_open_risk_pct}% cap of {aggregate_cap:,.2f}.",
            )
        )

    # Aggregate directional exposure. Individually compliant positions can
    # still stack into one large bet, which per-position gates cannot see.
    #
    # An unknown delta DENIES. It previously defaulted to 0.0 and was tested
    # for truthiness, so an unknown exposure skipped the cap entirely with no
    # denial and nothing in the audit log -- a cap that cannot fire and does
    # not say so is worse than no cap, because it reads as protection. A zero
    # delta is a real, meaningful value and is now distinguishable from
    # "we could not compute it".
    if net_delta_per_contract is None:
        reasons.append(
            (
                Denial.DELTA_UNKNOWN,
                "Directional exposure could not be computed, so the aggregate "
                "delta cap cannot be enforced. Refusing rather than trading "
                "with the cap silently inert.",
            )
        )
    else:
        delta_cap = limits.max_aggregate_net_delta_per_100k * (account.equity / 100_000)
        proposed_delta = contracts * net_delta_per_contract
        combined = account.net_delta + proposed_delta
        if abs(combined) > delta_cap:
            reasons.append(
                (
                    Denial.AGGREGATE_DELTA_CAP,
                    f"Book delta {account.net_delta:,.0f} plus proposed "
                    f"{proposed_delta:,.0f} is {combined:,.0f} share equivalents, "
                    f"beyond the {delta_cap:,.0f} cap. Individually compliant "
                    "positions would stack into one directional bet.",
                )
            )

    if proposed_risk > account.options_buying_power:
        reasons.append(
            (
                Denial.INSUFFICIENT_BUYING_POWER,
                f"Proposed risk {proposed_risk:,.2f} exceeds options buying "
                f"power {account.options_buying_power:,.2f}.",
            )
        )

    if reasons:
        return Decision.deny_all(reasons)
    return Decision(allowed=True, contracts=contracts)
