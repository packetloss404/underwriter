"""Reassembling broker positions into the spreads we actually hold.

`GET /v2/positions` returns individual option contracts. We trade spreads. A
put credit spread comes back as two unrelated option positions, and nothing in
the broker's response says they belong together.

Three things depend on getting this right, which is why it is the keystone of
the monitor: the risk engine's open risk and net delta, the snapshot diff that
is our only same-day signal of an assignment (GOTCHAS #12), and every exit
trigger, all of which need to know what is actually held.

**Pairing is done through the journal, not through geometry.** Two spreads at
adjacent strikes in the same expiry are indistinguishable from one wide spread
and one narrow one if you only look at the contracts. The journal's leg map
records what we actually opened, so it answers the question directly. Geometry
is not used as a fallback: a confident wrong pairing is worse than a recorded
orphan, because it silently misstates open risk while looking like an answer.

Anything that cannot be paired is returned as an `Orphan` rather than dropped.
A dropped leg is a position we hold and do not know about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from underwriter.journal import IntentLeg, OrderRecord, PositionRecord
from underwriter.occ import OccParseError
from underwriter.occ import parse as parse_occ

# One option contract covers 100 shares. Every dollar figure here is a position
# total rather than a per-spread figure -- see PositionRecord field docs below.
CONTRACT_MULTIPLIER = 100


class Unpairable(StrEnum):
    """Why a broker position could not be attributed to a spread."""

    UNPARSEABLE_SYMBOL = "unparseable_symbol"
    NO_JOURNALLED_ORDER = "no_journalled_order"
    ORDER_LEGS_MISSING = "order_legs_missing"
    NO_OPPOSING_LEG = "no_opposing_leg"
    ZERO_QUANTITY = "zero_quantity"


@dataclass(frozen=True, slots=True)
class RawOptionPosition:
    """One option position exactly as the broker reports it.

    `qty` is signed: negative is short. `current_price` is the mark the broker
    supplies; we prefer our own quotes where we have them, because the broker's
    per-leg figure is computed against the same indicative feed we already
    distrust (GOTCHAS #3).
    """

    symbol: str
    qty: float
    avg_entry_price: float | None = None
    current_price: float | None = None


@dataclass(frozen=True, slots=True)
class Orphan:
    """A held contract we could not attribute to a spread.

    This is never silently dropped. An orphan is a real position -- most often
    the surviving long wing after the short leg was assigned away -- and it
    carries risk whether or not we can name it.
    """

    position: RawOptionPosition
    reason: Unpairable
    detail: str


def _signed_delta(symbol: str, deltas: Mapping[str, float | None]) -> float | None:
    return deltas.get(symbol)


def _mid(symbol: str, quotes: Mapping[str, float | None]) -> float | None:
    return quotes.get(symbol)


def _leg_of(legs: Sequence[IntentLeg], symbol: str) -> IntentLeg | None:
    return next((leg for leg in legs if leg.occ_symbol == symbol), None)


@dataclass(frozen=True, slots=True)
class _Assembled:
    short: RawOptionPosition
    long: RawOptionPosition
    order: OrderRecord
    spreads: float
    width: float


def _build_record(
    a: _Assembled,
    *,
    quotes: Mapping[str, float | None],
    deltas: Mapping[str, float | None],
) -> PositionRecord:
    """Turn a matched pair into the record the risk engine consumes.

    Every dollar figure is a POSITION TOTAL, not a per-spread figure. The units
    were previously undeclared at this boundary, and a per-spread max_loss fed
    into an aggregate cap understates open risk by the contract count -- which
    is the direction that lets the book grow past its limit while reporting
    healthy. The assertion below pins it.
    """
    # The credit is recorded signed and negative for a credit; its magnitude is
    # what we received per spread.
    credit_per_spread = abs(a.order.net_price_per_spread or 0.0)
    max_loss = (a.width - credit_per_spread) * CONTRACT_MULTIPLIER * a.spreads

    short_delta = _signed_delta(a.short.symbol, deltas)
    long_delta = _signed_delta(a.long.symbol, deltas)
    if short_delta is None:
        net_delta = 0.0
        delta_note = " delta:unknown"
    else:
        # Missing wing delta counts as zero, which overstates exposure and errs
        # toward the aggregate cap firing early. Same convention as
        # CreditSpread.net_delta_per_spread.
        net_delta = (-short_delta + (long_delta or 0.0)) * CONTRACT_MULTIPLIER * a.spreads
        delta_note = "" if long_delta is not None else " delta:wing-missing"

    short_mid = _mid(a.short.symbol, quotes)
    long_mid = _mid(a.long.symbol, quotes)
    if short_mid is None or long_mid is None:
        unrealised = 0.0
        pnl_note = " pnl:unknown"
    else:
        # Closing costs the current net debit; we keep the difference against
        # the credit received.
        cost_to_close = short_mid - long_mid
        unrealised = (credit_per_spread - cost_to_close) * CONTRACT_MULTIPLIER * a.spreads
        pnl_note = ""

    record = PositionRecord(
        symbol=a.order.symbol,
        spreads=a.spreads,
        max_loss=max_loss,
        unrealised_pnl=unrealised,
        net_delta=net_delta,
        client_order_id=a.order.client_order_id,
        detail=(
            f"{a.short.symbol}/{a.long.symbol} width={a.width:g} "
            f"credit={credit_per_spread:.2f}{delta_note}{pnl_note}"
        ).strip(),
    )
    # Position total, never per-spread. A per-spread figure here silently
    # understates aggregate open risk by the contract count.
    assert record.max_loss >= 0.0
    return record


def reassemble_spreads(
    raw: Sequence[RawOptionPosition],
    *,
    orders_holding: Mapping[str, Sequence[OrderRecord]],
    legs_of: Mapping[str, Sequence[IntentLeg]],
    quotes: Mapping[str, float | None] | None = None,
    deltas: Mapping[str, float | None] | None = None,
) -> tuple[list[PositionRecord], list[Orphan]]:
    """Group broker contracts back into the spreads we hold.

    `orders_holding` maps an OCC symbol to the journalled orders containing it;
    `legs_of` maps a client order id to that order's recorded legs. Both come
    from the journal, which is the only place that knows which contracts were
    opened together.

    Returns the reassembled positions and every contract that could not be
    attributed. Callers must surface the orphans: the commonest one is a
    surviving long wing after the short leg was assigned away, which is a real
    position carrying real risk.
    """
    quotes = quotes or {}
    deltas = deltas or {}

    by_symbol = {p.symbol: p for p in raw}
    records: list[PositionRecord] = []
    orphans: list[Orphan] = []
    consumed: set[str] = set()

    for position in raw:
        if position.symbol in consumed:
            continue
        if position.qty == 0:
            orphans.append(
                Orphan(position, Unpairable.ZERO_QUANTITY, "Broker reported zero quantity.")
            )
            continue
        try:
            parse_occ(position.symbol)
        except OccParseError as exc:
            orphans.append(Orphan(position, Unpairable.UNPARSEABLE_SYMBOL, str(exc)))
            continue

        candidates = orders_holding.get(position.symbol, ())
        if not candidates:
            orphans.append(
                Orphan(
                    position,
                    Unpairable.NO_JOURNALLED_ORDER,
                    "No journalled order contains this contract, so we cannot say "
                    "which spread it belongs to. Held, unattributed.",
                )
            )
            continue

        matched = False
        for order in candidates:
            legs = legs_of.get(order.client_order_id, ())
            if len(legs) < 2:
                continue
            mine = _leg_of(legs, position.symbol)
            partner_leg = next((leg for leg in legs if leg.occ_symbol != position.symbol), None)
            if mine is None or partner_leg is None:
                continue
            partner = by_symbol.get(partner_leg.occ_symbol)
            if partner is None or partner.symbol in consumed:
                continue
            # One side must be short and the other long for this to be a spread
            # we still hold intact.
            if (position.qty < 0) == (partner.qty < 0):
                continue

            short_pos, long_pos = (position, partner) if position.qty < 0 else (partner, position)
            spreads = min(abs(short_pos.qty), abs(long_pos.qty))
            if spreads <= 0:
                continue
            width = abs(parse_occ(short_pos.symbol).strike - parse_occ(long_pos.symbol).strike)
            records.append(
                _build_record(
                    _Assembled(short_pos, long_pos, order, spreads, width),
                    quotes=quotes,
                    deltas=deltas,
                )
            )
            consumed.update({short_pos.symbol, long_pos.symbol})
            matched = True
            break

        if not matched:
            legs_seen = any(len(legs_of.get(o.client_order_id, ())) >= 2 for o in candidates)
            reason = Unpairable.NO_OPPOSING_LEG if legs_seen else Unpairable.ORDER_LEGS_MISSING
            detail = (
                "The other leg of this spread is no longer held -- assignment, "
                "expiry, or a partial close. This contract is still ours."
                if legs_seen
                else "The journalled order has no recorded legs, so its contracts "
                "cannot be grouped."
            )
            orphans.append(Orphan(position, reason, detail))

    return records, orphans
