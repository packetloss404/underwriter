"""A closing explanation requires the held order, exact legs and real time window."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from underwriter.execution_closures import plan_execution_closures
from underwriter.journal import (
    DerivedSpreadEvidence,
    ExecutionLegEvidence,
    IntentLeg,
    Journal,
    OrderStatus,
    PositionEventCause,
    PositionRecord,
)

NOW = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)


def scenario(
    journal: Journal,
    *,
    suffix: str = "a",
    at: datetime = NOW,
    expiry: str = "261030",
    close_price: float | None = 0.2,
) -> tuple[DerivedSpreadEvidence, tuple[ExecutionLegEvidence, ...], int]:
    short, long = f"XLE{expiry}P00097000", f"XLE{expiry}P00095000"
    opening, closing = "open-" + suffix, "close-" + suffix
    for identity, legs, price, moment in (
        (
            opening,
            (IntentLeg(short, "sell", 1, "sell_to_open"),
             IntentLeg(long, "buy", 1, "buy_to_open")),
            -0.5,
            at - timedelta(minutes=10),
        ),
        (
            closing,
            (IntentLeg(short, "buy", 1, "buy_to_close"),
             IntentLeg(long, "sell", 1, "sell_to_close")),
            close_price,
            at + timedelta(minutes=1),
        ),
    ):
        journal.record_intent(
            client_order_id=identity, cycle_id=identity, symbol="XLE", spreads=3,
            payload={}, legs=legs, at=moment,
        )
        journal.mark_status(
            identity, OrderStatus.FILLED, spreads_filled=3,
            net_price_per_spread=price, at=moment,
        )
    snapshot = journal.record_position_snapshot(
        [PositionRecord(
            symbol="XLE", spreads=3, max_loss=450, unrealised_pnl=0, net_delta=45,
            client_order_id=opening,
        )],
        at=at,
    )
    event = journal.record_position_event(
        symbol="XLE", cause=PositionEventCause.UNKNOWN, spreads=3,
        from_snapshot_id=snapshot, at=at + timedelta(minutes=5),
    )
    activities = tuple(
        ExecutionLegEvidence(
            activity_id="real-" + side + "-" + suffix,
            broker_order_id="child-" + side + "-" + suffix,
            client_order_id=closing,
            occ_symbol=symbol,
            side=side,
            contracts=3,
            premium_per_contract=premium,
            occurred_at=at + timedelta(minutes=2, microseconds=microsecond),
            metadata={},
        )
        for symbol, side, premium, microsecond in (
            (short, "buy", 0.3, 0), (long, "sell", 0.1, 1),
        )
    )
    group = DerivedSpreadEvidence(
        anchor_activity_id=activities[0].activity_id,
        client_order_id=closing, symbol="XLE", spreads=3, net_price_per_spread=0.2,
        occurred_at=activities[-1].occurred_at,
        activity_ids=tuple(row.activity_id for row in activities),
    )
    return group, activities, event.id


def test_matching_is_read_only_and_preserves_exact_event_and_execution_identity() -> None:
    with Journal() as journal:
        group, activities, event_id = scenario(journal)
        before = journal._conn.total_changes
        (match,) = plan_execution_closures(journal, [group], activities)
        assert journal._conn.total_changes == before
        assert match.position_event_id == event_id
        assert match.activity_id == activities[0].activity_id
        assert match.source_activity_ids == group.activity_ids
        assert match.opening_client_order_id == "open-a"
        assert match.closing_client_order_id == "close-a"
        assert match.occurred_at == activities[-1].occurred_at
        assert match.spreads == 3
        replay = plan_execution_closures(journal, [group, group], [*activities, *activities])
        assert replay == (match,)
        event = journal.unexplained_position_events()[0]
        journal.confirm_position_event(
            activity_id=match.activity_id, symbol="XLE", trading_day=event.trading_day,
            cause=PositionEventCause.CLOSED_BY_US, spreads=match.spreads,
            at=event.detected_at + timedelta(days=1),
        )
        assert plan_execution_closures(journal, [group], activities) == ()
        persisted = journal.position_events()[0]
        assert persisted.id == event_id
        assert persisted.detected_at == event.detected_at
        assert persisted.from_snapshot_id == match.source_snapshot_id


@pytest.mark.parametrize("kind", ["before", "after", "foreign", "side", "quantity", "price"])
def test_any_unproven_component_prevents_a_close_explanation(kind: str) -> None:
    with Journal() as journal:
        group, activities, _ = scenario(journal)
        first = activities[0]
        if kind == "before":
            first = replace(first, occurred_at=NOW - timedelta(microseconds=1))
        elif kind == "after":
            first = replace(first, occurred_at=NOW + timedelta(minutes=6))
        elif kind == "foreign":
            first = replace(first, occ_symbol="XLE261106P00097000")
        elif kind == "side":
            first = replace(first, side="sell")
        elif kind == "quantity":
            first = replace(first, contracts=2)
        else:
            first = replace(first, premium_per_contract=0.4)
        assert plan_execution_closures(journal, [group], [first, activities[1]]) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"anchor_activity_id": "invented"},
        {"activity_ids": ("missing",)},
        {"spreads": 2},
        {"net_price_per_spread": 0.25},
        {"occurred_at": NOW + timedelta(minutes=2)},
        {"client_order_id": "open-a"},
    ],
)
def test_derived_group_cannot_replace_actual_source_proof(change: dict[str, Any]) -> None:
    with Journal() as journal:
        group, activities, _ = scenario(journal)
        assert plan_execution_closures(journal, [replace(group, **change)], activities) == ()


def test_parent_missing_price_remains_unexplained() -> None:
    with Journal() as journal:
        group, activities, _ = scenario(journal, close_price=None)
        assert plan_execution_closures(journal, [group], activities) == ()


def test_same_symbol_same_day_uses_source_opening_contracts_and_snapshot_interval() -> None:
    with Journal() as journal:
        first, first_activities, first_event = scenario(journal)
        second, second_activities, second_event = scenario(
            journal, suffix="b", at=NOW + timedelta(minutes=10), expiry="261106"
        )
        matches = plan_execution_closures(
            journal, [second, first], [*second_activities, *first_activities]
        )
        assert {match.position_event_id: match.closing_client_order_id for match in matches} == {
            first_event: "close-a", second_event: "close-b",
        }


def test_multiple_possible_closing_orders_or_reused_source_events_stay_unknown() -> None:
    with Journal() as journal:
        first, first_activities, _ = scenario(journal)
        second, second_activities, _ = scenario(journal, suffix="b")
        # Both identical-contract closes fit both disappearance windows.
        assert plan_execution_closures(
            journal, [first, second], [*first_activities, *second_activities]
        ) == ()


def test_opening_order_or_incorrect_closing_intent_cannot_explain_disappearance() -> None:
    with Journal() as journal:
        group, activities, _ = scenario(journal)
        journal._conn.execute(
            "UPDATE order_legs SET position_intent='buy_to_open' "
            "WHERE client_order_id='close-a' AND side='buy'"
        )
        assert plan_execution_closures(journal, [group], activities) == ()


def test_conflicting_source_identity_is_rejected_instead_of_last_row_winning() -> None:
    with Journal() as journal:
        group, activities, _ = scenario(journal)
        conflicting = replace(activities[0], premium_per_contract=0.4)
        with pytest.raises(ValueError, match="Conflicting"):
            plan_execution_closures(journal, [group], [*activities, conflicting])
