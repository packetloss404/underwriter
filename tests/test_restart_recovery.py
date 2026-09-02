"""Deterministic proof for the Railway restart boundary.

Railway already proved that the mounted volume survives a container redeploy.
This test supplies the missing half of that evidence: state on that volume is
enough to resume a live consequence and the broker-isolated exploratory lane
across fresh Journal connections, without repeating either consequence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.test_cycle import (
    NOW,
    XLE_SOON_LONG,
    XLE_SOON_SHORT,
    FakeBroker,
    FakeMarket,
    FakeOrders,
    build,
    hold_xle,
    market_for,
    passing_preflight,
    snapshot,
)
from underwriter.cycle import BrokerOrderView, Refusal
from underwriter.execution import Backend, OrderResult
from underwriter.journal import (
    FillSource,
    Journal,
    OrderStatus,
    PnlSource,
    PositionEventCause,
)
from underwriter.positions import RawOptionPosition


def _held_broker() -> FakeBroker:
    return FakeBroker(
        raw=[
            RawOptionPosition(XLE_SOON_SHORT, -3.0),
            RawOptionPosition(XLE_SOON_LONG, 3.0),
        ]
    )


def _market(at: datetime = NOW) -> FakeMarket:
    market = market_for(("XLE",), at=at)
    market.snapshots = {
        XLE_SOON_SHORT: snapshot(0.26, 0.30, delta=-0.20, at=at),
        XLE_SOON_LONG: snapshot(0.02, 0.06, delta=-0.08, at=at),
    }
    return market


def test_restart_recovers_open_state_without_repeating_or_stranding_consequences(
    tmp_path: Path,
) -> None:
    database = tmp_path / "railway-volume" / "underwriter.db"
    database.parent.mkdir()

    # Deployment A: observe the live spread and submit its expiry-driven close.
    # The broker accepts it but it remains working when the process is stopped.
    with Journal(database) as journal:
        market = _market()
        broker = _held_broker()
        hold_xle(
            journal,
            market,
            broker,
            exit_ask=0.30,
            short=XLE_SOON_SHORT,
            long_=XLE_SOON_LONG,
        )
        cycle, _, _, executor = build(
            journal,
            market=market,
            broker=broker,
            kill_switch=True,
        )
        executor.results["close"] = OrderResult(
            ok=True,
            backend=Backend.SDK,
            client_order_id="ignored",
            payload={},
            order_id="broker-close-1",
            status="accepted",
            filled_qty=Decimal("0"),
            at=NOW,
        )
        first = cycle.run(preflight=passing_preflight())
        (close,) = first.closed
        close_id = close.client_order_id
        close_record = journal.order(close_id)
        assert close_record is not None
        assert close_record.status is OrderStatus.ACCEPTED

        # The September 1 lane is stateful too, but it must never create a
        # broker consequence. Leave one open at the same restart boundary.
        exploratory = journal.open_exploratory_position(
            cycle_id="explore-before-restart",
            symbol="XLE",
            short_symbol=XLE_SOON_SHORT,
            long_symbol=XLE_SOON_LONG,
            expiry=NOW.date() + timedelta(days=2),
            spreads=1,
            width=2.0,
            credit_per_spread=0.50,
            max_loss=150.0,
            net_delta=15.0,
            opening_vrp_ratio=1.08,
            at=NOW,
        )

    # Deployment B: a new connection reads both open states from the file.
    # Reconciliation keeps the accepted close live, the exit stage refuses to
    # submit it twice, and the exploratory expiry closes without broker I/O.
    second_at = NOW + timedelta(minutes=5)
    with Journal(database) as journal:
        assert [order.client_order_id for order in journal.unreconciled_orders()] == [close_id]
        recovered_exploratory = journal.exploratory_open_position()
        assert recovered_exploratory is not None
        assert recovered_exploratory.id == exploratory.id
        orders = FakeOrders(
            views={
                close_id: BrokerOrderView(
                    status="accepted",
                    order_id="broker-close-1",
                    filled_qty=0.0,
                )
            }
        )
        cycle, _, _, executor = build(
            journal,
            at=second_at,
            market=_market(second_at),
            broker=_held_broker(),
            orders=orders,
            kill_switch=True,
        )
        second = cycle.run(preflight=passing_preflight())
        assert executor.calls == []
        assert orders.asked == [close_id]
        assert any(Refusal.EXIT_ALREADY_WORKING.value in row.reasons for row in second.rejections)
        assert journal.exploratory_open_position() is None
        exploratory_rows = journal.exploratory_positions()
        assert len(exploratory_rows) == 1
        assert exploratory_rows[0].id == exploratory.id
        assert exploratory_rows[0].realised_pnl == pytest.approx(22.0)

        # The authoritative REST activity arrives before the next restart.
        # Its broker execution id is the idempotency boundary for the effect.
        assert journal.record_spread_fill(
            fill_id="execution-close-1",
            client_order_id=close_id,
            broker_order_id="broker-close-1",
            symbol="XLE",
            spreads=3.0,
            net_price_per_spread=0.24,
            occurred_at=second_at + timedelta(minutes=1),
            source=FillSource.REST,
            at=second_at + timedelta(minutes=1),
        )

    # Deployment C: the broker is flat. The durable fill explains the vanished
    # position exactly once and the accepted order reaches a terminal state.
    third_at = second_at + timedelta(minutes=2)
    with Journal(database) as journal:
        orders = FakeOrders(
            views={
                close_id: BrokerOrderView(
                    status="filled",
                    order_id="broker-close-1",
                    filled_qty=3.0,
                    filled_avg_price=0.24,
                )
            }
        )
        cycle, _, _, executor = build(
            journal,
            at=third_at,
            market=_market(third_at),
            broker=FakeBroker(raw=[]),
            orders=orders,
            kill_switch=True,
        )
        cycle.run(preflight=passing_preflight())
        assert executor.calls == []
        assert journal.unreconciled_orders() == ()
        close_record = journal.order(close_id)
        assert close_record is not None
        assert close_record.status is OrderStatus.FILLED
        events = journal.position_events()
        assert len(events) == 1
        assert events[0].cause is PositionEventCause.CLOSED_BY_US

    # Deployment D: replaying the fully recovered state creates no second
    # close, fill, position event, or exploratory result. Nothing is stranded.
    fourth_at = third_at + timedelta(minutes=2)
    with Journal(database) as journal:
        orders = FakeOrders()
        cycle, _, _, executor = build(
            journal,
            at=fourth_at,
            market=_market(fourth_at),
            broker=FakeBroker(raw=[]),
            orders=orders,
            kill_switch=True,
        )
        report = cycle.run(preflight=passing_preflight())
        assert executor.calls == []
        assert orders.asked == []
        assert report.closed == ()
        assert journal.unreconciled_orders() == ()
        assert len(journal.spread_fills_for(close_id)) == 1
        assert len(journal.position_events()) == 1
        assert len(journal.exploratory_positions()) == 1
        assert journal.latest_pnl(
            trading_day=fourth_at.date(), source=PnlSource.EXPLORATORY
        ) is not None
