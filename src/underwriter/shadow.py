"""The conservative P&L series, and why it is the one to believe.

Alpaca's paper engine simulates fills against *modified indicative quotes*, and
the multi-leg fill model is undocumented (docs/GOTCHAS.md #3). So the official
number is a number we report, not a number we trust: it can fill us at prices a
real book would not have offered, and it never charges us for crossing a
spread.

The shadow series recomputes the same trades under assumptions we are willing
to defend out loud.

**Rule one: never better than we asked.** Our submitted limit already embeds a
conservative estimate -- the price was built assuming we cross half of each
leg's quoted spread. A paper fill that beats that limit is the simulator being
generous, not the market. Shadow prices every fill at the worse of what we got
and what we asked for.

Because a credit is negative and a debit positive, "worse for us" is always
"more positive", so that rule is `max(actual, limit)` in signed terms and needs
no branch on direction. An opening credit of -1.30 against a -1.20 limit shadows
to -1.20; a closing debit of 0.30 against a 0.40 limit shadows to 0.40.

**Rule two: a fixed execution haircut per spread.** Even at our own limit, a
real fill pays exchange and regulatory fees the paper engine does not model,
and the indicative quote the limit was derived from may itself have been wrong.
The haircut is small, per-spread, and always against us.

The result is a number that can only be worse than reality, never better. That
is the point: a strategy judged on four sessions should be reported at the
bottom of its plausible range, not the top.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from underwriter.journal import Journal, OrderRecord, SpreadFill
from underwriter.positions import CONTRACT_MULTIPLIER

# Charged per spread on every fill, in dollars, always against us.
#
# Options carry per-contract exchange and regulatory fees that a paper engine
# does not model, and a two-leg spread pays them twice. Five cents a spread is
# deliberately more than the real figure: the purpose of this series is to be
# defensible, and a haircut that is too small is worse than none because it
# implies a precision we do not have.
EXECUTION_HAIRCUT_PER_SPREAD = 0.05


@dataclass(frozen=True, slots=True)
class ShadowFill:
    """One fill, repriced conservatively."""

    fill_id: str
    symbol: str
    spreads: float
    actual_net_price: float
    limit_net_price: float | None
    shadow_net_price: float

    @property
    def improved_by_the_simulator(self) -> bool:
        """Whether the paper engine filled us better than we asked."""
        return self.limit_net_price is not None and self.actual_net_price < self.limit_net_price

    @property
    def give_up_usd(self) -> float:
        """Dollars the shadow series declines to claim on this fill."""
        return (self.shadow_net_price - self.actual_net_price) * CONTRACT_MULTIPLIER * self.spreads


@dataclass(frozen=True, slots=True)
class ShadowPnl:
    """The conservative series for one trading day."""

    trading_day: date
    realised_usd: float
    official_realised_usd: float | None
    fills: tuple[ShadowFill, ...]
    haircut_usd: float
    unpriced_fills: int

    @property
    def gap_usd(self) -> float | None:
        """How much rosier the official number is. None when it is unknown."""
        if self.official_realised_usd is None:
            return None
        return self.official_realised_usd - self.realised_usd

    @property
    def complete(self) -> bool:
        """Whether every fill could be repriced.

        False means some fill had no recorded limit to compare against, so the
        shadow figure is missing a haircut it should have taken and is
        therefore *less* conservative than it claims. Callers must surface
        this rather than presenting the number as final.
        """
        return self.unpriced_fills == 0


def _limit_from(order: OrderRecord | None) -> float | None:
    """The signed limit price we submitted, read out of the stored payload.

    Returns None when the order is unknown or the payload does not carry a
    readable limit -- a broker-initiated fill has no order of ours behind it,
    and inventing a limit for it would manufacture the very conservatism this
    module exists to measure.
    """
    if order is None:
        return None
    payload: object = order.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("limit_price")
    if isinstance(raw, bool) or raw is None:
        # bool subclasses int, and True would read as a 1.00 limit.
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def shadow_price(actual: float, limit: float | None) -> float:
    """The worse of what we got and what we asked for.

    Signed: a credit is negative, a debit positive, so worse is always more
    positive and this needs no branch on trade direction.
    """
    return actual if limit is None else max(actual, limit)


def reprice(fill: SpreadFill, order: OrderRecord | None) -> ShadowFill:
    """Reprice one fill conservatively."""
    limit = _limit_from(order)
    return ShadowFill(
        fill_id=fill.fill_id,
        symbol=fill.symbol,
        spreads=fill.spreads,
        actual_net_price=fill.net_price_per_spread,
        limit_net_price=limit,
        shadow_net_price=shadow_price(fill.net_price_per_spread, limit),
    )


def realised(
    fills: Sequence[ShadowFill], *, haircut: float = EXECUTION_HAIRCUT_PER_SPREAD
) -> float:
    """Realised P&L across a set of repriced fills.

    Uses the same negated-sum form as the journal's own P&L: the sign
    convention is applied once to every price rather than by subtracting a
    debit from a credit, which silently inverts for debit structures.
    """
    gross = -sum(f.shadow_net_price * CONTRACT_MULTIPLIER * f.spreads for f in fills)
    fees = sum(abs(f.spreads) for f in fills) * haircut * CONTRACT_MULTIPLIER
    return gross - fees


def shadow_for_day(
    journal: Journal,
    trading_day: date,
    *,
    haircut: float = EXECUTION_HAIRCUT_PER_SPREAD,
) -> ShadowPnl:
    """Compute the conservative series for one trading day."""
    fills = journal.spread_fills_on(trading_day)
    orders: dict[str, OrderRecord | None] = {}
    repriced: list[ShadowFill] = []
    for fill in fills:
        cid = fill.client_order_id
        if cid and cid not in orders:
            orders[cid] = journal.order(cid)
        repriced.append(reprice(fill, orders.get(cid) if cid else None))

    official = journal.latest_pnl(trading_day=trading_day)
    fee_total = sum(abs(f.spreads) for f in repriced) * haircut * CONTRACT_MULTIPLIER

    return ShadowPnl(
        trading_day=trading_day,
        realised_usd=realised(repriced, haircut=haircut),
        official_realised_usd=None if official is None else official.realised_pnl,
        fills=tuple(repriced),
        haircut_usd=fee_total,
        unpriced_fills=sum(1 for f in repriced if f.limit_net_price is None),
    )
