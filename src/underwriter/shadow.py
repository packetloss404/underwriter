"""Execution-adjusted broker daily observations, with explicit coverage.

The official snapshot stores broker equity change since the previous close
less the current book's recorded unrealised mark. Its realised_pnl field is a
broker-derived residual, not independently reconstructed closed-trade profit.

The published shadow subtracts confirmed fill-price improvements surrendered
at our limit and a modelled $0.05 cost per spread per execution. It shares the
official snapshot's open mark. Thus its total is broker daily equity change
less these adjustments; opening cash credits are not counted as new profit.

Signed prices make the surrendered improvement max(actual, limit) - actual.
A -1.30 opening fill against a -1.20 limit gives up $10 per spread. Missing
limits, unconfirmed execution events, incomplete parent quantities or
missing/stale official marks prevent publication. No activity IDs are inferred.

These assumptions reduce the corresponding broker observation. They are not
executable-market prices, a fee estimate or a guarantee about real returns.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from underwriter.journal import Journal, OrderRecord, SpreadFill
from underwriter.positions import CONTRACT_MULTIPLIER

# Modelled dollars per spread per execution, not option premium points.
# This is an explicit assumption, not a measured exchange or broker fee.
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
    """The adjusted broker daily residual and its execution coverage."""

    trading_day: date
    realised_usd: float
    official_realised_usd: float | None
    fills: tuple[ShadowFill, ...]
    haircut_usd: float
    unpriced_fills: int
    missing_order_fills: int = 0
    unconfirmed_fills: int = 0
    official_missing: bool = False

    @property
    def gap_usd(self) -> float | None:
        """How much rosier the official number is. None when it is unknown."""
        if self.official_realised_usd is None:
            return None
        return self.official_realised_usd - self.realised_usd

    @property
    def complete(self) -> bool:
        """Whether current observations and confirmed executions support publication."""
        return (
            self.unpriced_fills == 0
            and self.missing_order_fills == 0
            and self.unconfirmed_fills == 0
            and not self.official_missing
        )


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
    """Net cash flow across repriced fills, less modelled dollar execution cost.

    This equals trade realised P&L only for a complete closed/expired lifecycle.
    The daily published shadow uses broker accounting instead of this helper.
    """
    gross = -sum(f.shadow_net_price * CONTRACT_MULTIPLIER * f.spreads for f in fills)
    fees = sum(abs(f.spreads) for f in fills) * haircut
    return gross - fees


def shadow_for_day(
    journal: Journal,
    trading_day: date,
    *,
    haircut: float = EXECUTION_HAIRCUT_PER_SPREAD,
) -> ShadowPnl:
    """Adjust broker realised P&L by confirmed execution-price penalties.

    Missing/partial events or an official mark predating known fills make the
    result incomplete. Callers must not publish an incomplete result.
    """
    fills = journal.spread_fills_on(trading_day)
    orders: dict[str, OrderRecord | None] = {}
    repriced: list[ShadowFill] = []
    for fill in fills:
        cid = fill.client_order_id
        if cid and cid not in orders:
            orders[cid] = journal.order(cid)
        repriced.append(reprice(fill, orders.get(cid) if cid else None))

    official = journal.latest_pnl(trading_day=trading_day)
    fee_total = sum(abs(f.spreads) for f in repriced) * haircut
    missing_orders = journal.uncovered_filled_orders(trading_day)
    latest_fill_observation = max(
        (
            order.status_at or order.submitted_at or order.intent_at
            for order in journal.filled_order_observations(trading_day)
        ),
        default=None,
    )
    official_missing = (
        bool(fills or missing_orders) and official is None
    ) or (
        official is not None
        and (
            (latest_fill_observation is not None and latest_fill_observation > official.at)
            or any(fill.occurred_at > official.at for fill in fills)
        )
    )
    # Broker realised P&L already excludes the current book's mark-to-market.
    # Subtract only simulator improvement and dollar fees here; summing opening
    # credits as realised would count that premium again while still open.
    base = 0.0 if official is None else official.realised_pnl
    adjusted = base - sum(fill.give_up_usd for fill in repriced) - fee_total

    return ShadowPnl(
        trading_day=trading_day,
        realised_usd=adjusted,
        official_realised_usd=None if official is None else official.realised_pnl,
        fills=tuple(repriced),
        haircut_usd=fee_total,
        unpriced_fills=sum(1 for f in repriced if f.limit_net_price is None),
        missing_order_fills=len(missing_orders),
        unconfirmed_fills=sum(1 for fill in fills if fill.confirmed_at is None),
        official_missing=official_missing,
    )
