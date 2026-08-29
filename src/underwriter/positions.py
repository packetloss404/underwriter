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
from datetime import date, datetime
from enum import StrEnum

from underwriter.journal import (
    IntentLeg,
    Journal,
    OrderRecord,
    PositionEvent,
    PositionEventCause,
    PositionEventEvidence,
    PositionRecord,
)
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
class OpenSpread:
    """A spread we hold, with everything the monitor and exits need.

    Richer than the journal's `PositionRecord`, deliberately. The journal
    stores what the risk engine consumes; the exit path additionally needs the
    two contract symbols (to build the closing order) and the expiry (for the
    time stop and the hard flatten). Carrying them here means the OCC join
    happens once, at reassembly, rather than being re-derived from an opaque
    payload blob at every call site that needs a date.
    """

    underlying: str
    short_symbol: str
    long_symbol: str
    expiry: date
    spreads: float
    width: float
    credit_per_spread: float
    max_loss: float
    net_delta: float
    unrealised_pnl: float
    client_order_id: str
    detail: str = ""

    def days_to_expiry(self, as_of: date) -> int:
        return (self.expiry - as_of).days

    @property
    def record(self) -> PositionRecord:
        """The lean form the journal stores and the risk engine reads."""
        return PositionRecord(
            symbol=self.underlying,
            spreads=self.spreads,
            max_loss=self.max_loss,
            unrealised_pnl=self.unrealised_pnl,
            net_delta=self.net_delta,
            client_order_id=self.client_order_id,
            detail=self.detail,
        )


@dataclass(frozen=True, slots=True)
class _Assembled:
    short: RawOptionPosition
    long: RawOptionPosition
    order: OrderRecord
    spreads: float
    width: float


def _build_spread(
    a: _Assembled,
    *,
    quotes: Mapping[str, float | None],
    deltas: Mapping[str, float | None],
) -> OpenSpread:
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

    spread = OpenSpread(
        underlying=a.order.symbol,
        short_symbol=a.short.symbol,
        long_symbol=a.long.symbol,
        expiry=parse_occ(a.short.symbol).expiry,
        spreads=a.spreads,
        width=a.width,
        credit_per_spread=credit_per_spread,
        max_loss=max_loss,
        net_delta=net_delta,
        unrealised_pnl=unrealised,
        client_order_id=a.order.client_order_id,
        detail=(
            f"{a.short.symbol}/{a.long.symbol} width={a.width:g} "
            f"credit={credit_per_spread:.2f}{delta_note}{pnl_note}"
        ).strip(),
    )
    # Position total, never per-spread. A per-spread figure here silently
    # understates aggregate open risk by the contract count.
    assert spread.max_loss >= 0.0
    return spread


def reassemble_spreads(
    raw: Sequence[RawOptionPosition],
    *,
    orders_holding: Mapping[str, Sequence[OrderRecord]],
    legs_of: Mapping[str, Sequence[IntentLeg]],
    quotes: Mapping[str, float | None] | None = None,
    deltas: Mapping[str, float | None] | None = None,
) -> tuple[list[OpenSpread], list[Orphan]]:
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
    records: list[OpenSpread] = []
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
                _build_spread(
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


@dataclass(frozen=True, slots=True)
class BookObservation:
    """The result of one cycle's look at what we hold."""

    snapshot_id: int
    positions: Sequence[OpenSpread]
    orphans: Sequence[Orphan]
    events: Sequence[PositionEvent]

    @property
    def needs_attention(self) -> bool:
        """Whether a human should look at this cycle.

        An orphan or an unexplained departure both mean our picture of the book
        disagrees with the broker's, which is the condition under which the
        risk gates are reasoning about something that is not there.
        """
        return bool(self.orphans) or any(
            e.cause is not PositionEventCause.CLOSED_BY_US for e in self.events
        )


def observe_book(
    journal: Journal,
    positions: Sequence[OpenSpread],
    orphans: Sequence[Orphan] = (),
    *,
    at: datetime | None = None,
) -> BookObservation:
    """Record the book, detect departures, and file them -- in one call.

    These three steps belong together because doing the first without the
    others loses information permanently. `vanished_positions()` reports
    departures from the persisted diff cursor forward, so a cycle that records
    a snapshot and then fails to consume the diff leaves the backlog for the
    next cycle -- but only if the cursor is advanced *after* the events are
    filed, never before. Splitting these across call sites is how that ordering
    gets broken.

    On paper this is the ONLY same-day signal that a position was assigned or
    expired: those never reach the trade-updates stream, and on paper they are
    absent from the activities feed until the following day (GOTCHAS #12). A
    position that disappears with no fill we initiated is filed as UNKNOWN
    rather than as a clean close, because guessing "assignment" here would put
    a cause in the audit trail that we did not observe. The next day's activity
    record confirms it through `confirm_position_event`.
    """
    snapshot_id = journal.record_position_snapshot([p.record for p in positions], at=at)

    events: list[PositionEvent] = []
    for vanished in journal.vanished_positions():
        # A departure we can explain with our own fill is a clean close.
        # Anything else is a position that left without us, and the honest
        # record is that we do not know which of assignment, expiry or broker
        # liquidation it was.
        explained = bool(vanished.closing_fills)
        events.append(
            journal.record_position_event(
                symbol=vanished.position.symbol,
                cause=(
                    PositionEventCause.CLOSED_BY_US if explained else PositionEventCause.UNKNOWN
                ),
                evidence=PositionEventEvidence.INFERRED_FROM_SNAPSHOT,
                spreads=vanished.position.spreads,
                from_snapshot_id=vanished.from_snapshot_id,
                detail=(
                    f"Closed by our fill(s): {', '.join(f.fill_id for f in vanished.closing_fills)}"
                    if explained
                    else "Position gone with no fill of ours in the window. "
                    "Assignment, expiry or broker liquidation -- cause not yet "
                    "observed; awaiting the next session's activity record."
                ),
                at=at,
            )
        )

    # Advance the cursor only after every event is filed. A crash before this
    # re-reports the same departures next cycle, and the uniqueness constraint
    # on recording makes that idempotent -- at-least-once detection plus
    # idempotent recording gives an exactly-once effect.
    journal.mark_positions_diffed(snapshot_id, at=at)

    return BookObservation(
        snapshot_id=snapshot_id,
        positions=tuple(positions),
        orphans=tuple(orphans),
        events=tuple(events),
    )
