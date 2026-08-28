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

from rotunda.config import RiskLimits
from rotunda.universe import are_correlated, is_tradeable


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


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """An open defined-risk position, as the risk engine needs to see it."""

    symbol: str
    max_loss: float
    unrealised_pnl: float = 0.0


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
    starting_equity: float
    realised_pnl_today: float = 0.0
    open_positions: Sequence[OpenPosition] = field(default_factory=tuple)

    @property
    def open_risk(self) -> float:
        return sum(p.max_loss for p in self.open_positions)

    @property
    def unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl for p in self.open_positions)

    @property
    def conservative_day_pnl(self) -> float:
        """Realised plus unrealised.

        Unrealised losses count against the daily stop; unrealised *gains* do
        not offset it. A stop that a paper profit can unlock is not a stop.
        """
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
    if account.starting_equity > 0:
        loss_limit = account.starting_equity * (limits.daily_loss_stop_pct / 100)
        if account.conservative_day_pnl <= -loss_limit:
            reasons.append(
                (
                    Denial.DAILY_LOSS_STOP,
                    f"Day P&L {account.conservative_day_pnl:,.2f} breaches the "
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
