"""Plan strict, read-only attribution of vanished positions to real closing legs.

A symbol and a date never establish a close. The source held snapshot names
the opening order, whose exact contracts and ratios must be reversed by a
journalled close. Every actual component execution must fall inside that
snapshot's disappearance interval. Ambiguous candidates remain unexplained.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from underwriter.journal import (
    DerivedSpreadEvidence,
    ExecutionLegEvidence,
    IntentLeg,
    Journal,
    PositionEvent,
    PositionEventCause,
    PositionEventEvidence,
)

_EPSILON = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class ExecutionClosureMatch:
    """A proposed update of one existing event, anchored to real source IDs."""

    position_event_id: int
    activity_id: str
    closing_client_order_id: str
    opening_client_order_id: str
    source_snapshot_id: int
    source_activity_ids: tuple[str, ...]
    spreads: float
    occurred_at: datetime


def _same_number(first: float, second: float) -> bool:
    return (
        math.isfinite(first)
        and math.isfinite(second)
        and abs(Decimal(str(first)) - Decimal(str(second))) <= _EPSILON
    )


def _opening_legs(legs: Sequence[IntentLeg]) -> dict[str, IntentLeg] | None:
    mapped = {leg.occ_symbol: leg for leg in legs}
    if len(mapped) != len(legs) or len(mapped) < 2:
        return None
    for leg in mapped.values():
        expected = "sell_to_open" if leg.side == "sell" else "buy_to_open"
        if leg.side not in ("buy", "sell") or leg.position_intent != expected or leg.ratio_qty < 1:
            return None
    return mapped


def _reverses(opening: dict[str, IntentLeg], closing: Sequence[IntentLeg]) -> bool:
    mapped = {leg.occ_symbol: leg for leg in closing}
    if set(mapped) != set(opening) or len(mapped) != len(closing):
        return False
    for symbol, original in opening.items():
        close = mapped[symbol]
        side = "buy" if original.side == "sell" else "sell"
        intent = "buy_to_close" if original.side == "sell" else "sell_to_close"
        if close.side != side or close.position_intent != intent:
            return False
        if close.ratio_qty != original.ratio_qty:
            return False
    return True


def _candidate(
    journal: Journal,
    event: PositionEvent,
    group: DerivedSpreadEvidence,
    activities: dict[str, ExecutionLegEvidence],
) -> ExecutionClosureMatch | None:
    if (
        event.from_snapshot_id is None
        or group.symbol != event.symbol
        or not _same_number(group.spreads, event.spreads)
        or not math.isfinite(group.spreads)
        or group.spreads <= 0
    ):
        return None
    row = journal._conn.execute(
        "SELECT p.client_order_id,p.spreads,s.taken_at FROM snapshot_positions p "
        "JOIN position_snapshots s ON s.id=p.snapshot_id "
        "WHERE p.snapshot_id=? AND p.symbol=?",
        (event.from_snapshot_id, event.symbol),
    ).fetchone()
    if row is None or row["client_order_id"] is None:
        return None
    opening_id = str(row["client_order_id"])
    if opening_id == group.client_order_id or not _same_number(
        float(row["spreads"]), event.spreads
    ):
        return None
    snapshot_at = datetime.fromisoformat(str(row["taken_at"]))
    if snapshot_at.tzinfo is None or event.detected_at.tzinfo is None:
        return None
    opening = journal.order(opening_id)
    closing = journal.order(group.client_order_id)
    if (
        opening is None
        or closing is None
        or opening.symbol != event.symbol
        or closing.symbol != event.symbol
        or closing.net_price_per_spread is None
        or not _same_number(closing.spreads_filled, event.spreads)
        or not _same_number(closing.net_price_per_spread, group.net_price_per_spread)
    ):
        return None
    original_legs = _opening_legs(journal.legs_for(opening_id))
    closing_legs = journal.legs_for(group.client_order_id)
    if original_legs is None or not _reverses(original_legs, closing_legs):
        return None
    ids = group.activity_ids
    if (
        not ids
        or len(set(ids)) != len(ids)
        or group.anchor_activity_id not in ids
        or any(identity not in activities for identity in ids)
    ):
        return None
    components = [activities[identity] for identity in ids]
    expected = {leg.occ_symbol: leg for leg in closing_legs}
    quantities = dict.fromkeys(expected, Decimal(0))
    cash = Decimal(0)
    for activity in components:
        leg = expected.get(activity.occ_symbol)
        if (
            activity.client_order_id != group.client_order_id
            or not activity.broker_order_id
            or leg is None
            or activity.side != leg.side
            or not math.isfinite(activity.contracts)
            or activity.contracts <= 0
            or activity.contracts != math.floor(activity.contracts)
            or not math.isfinite(activity.premium_per_contract)
            or activity.premium_per_contract < 0
            or activity.occurred_at.tzinfo is None
            or not snapshot_at <= activity.occurred_at <= event.detected_at
        ):
            return None
        contracts = Decimal(str(activity.contracts))
        quantities[activity.occ_symbol] += contracts
        direction = Decimal(1) if activity.side == "buy" else Decimal(-1)
        cash += direction * contracts * Decimal(str(activity.premium_per_contract))
    if any(
        abs(quantities[symbol] - Decimal(leg.ratio_qty) * Decimal(str(event.spreads))) > _EPSILON
        for symbol, leg in expected.items()
    ):
        return None
    latest = max(activity.occurred_at for activity in components)
    if group.occurred_at != latest:
        return None
    derived_price = cash / Decimal(str(group.spreads))
    if abs(derived_price - Decimal(str(group.net_price_per_spread))) > _EPSILON:
        return None
    return ExecutionClosureMatch(
        position_event_id=event.id,
        activity_id=group.anchor_activity_id,
        closing_client_order_id=group.client_order_id,
        opening_client_order_id=opening_id,
        source_snapshot_id=event.from_snapshot_id,
        source_activity_ids=ids,
        spreads=event.spreads,
        occurred_at=latest,
    )


def plan_execution_closures(
    journal: Journal,
    groups: Sequence[DerivedSpreadEvidence],
    activities: Sequence[ExecutionLegEvidence],
) -> tuple[ExecutionClosureMatch, ...]:
    """Return only unique, source-bound matches; never write journal state."""
    by_id: dict[str, ExecutionLegEvidence] = {}
    for activity in activities:
        existing = by_id.get(activity.activity_id)
        if existing is not None and existing != activity:
            raise ValueError("Conflicting closure execution identities")
        by_id[activity.activity_id] = activity
    unique_groups: dict[tuple[str, tuple[str, ...]], DerivedSpreadEvidence] = {}
    for group in groups:
        key = (group.anchor_activity_id, tuple(sorted(group.activity_ids)))
        existing_group = unique_groups.get(key)
        if existing_group is not None and existing_group != group:
            raise ValueError("Conflicting closure group evidence")
        unique_groups[key] = group
    candidates: list[tuple[ExecutionClosureMatch, ...]] = []
    for event in journal.unexplained_position_events():
        if (
            event.cause is not PositionEventCause.UNKNOWN
            or event.evidence is not PositionEventEvidence.INFERRED_FROM_SNAPSHOT
            or event.activity_id is not None
        ):
            continue
        matches = tuple(
            match
            for group in unique_groups.values()
            if (match := _candidate(journal, event, group, by_id)) is not None
        )
        if matches:
            candidates.append(matches)
    anchor_uses = Counter(match.activity_id for matches in candidates for match in matches)
    source_uses = Counter(
        identity
        for matches in candidates
        for match in matches
        for identity in match.source_activity_ids
    )
    return tuple(
        matches[0]
        for matches in candidates
        if len(matches) == 1
        and anchor_uses[matches[0].activity_id] == 1
        and all(source_uses[identity] == 1 for identity in matches[0].source_activity_ids)
    )
