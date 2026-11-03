"""Broker-shaped GET responses for production ingestion browser fixtures."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from tests.test_dashboard import LONG_LEG, NOW, SHORT_LEG

from underwriter.execution_events import ActivityReader
from underwriter.execution_ingestion import ExecutionIngestor
from underwriter.journal import IntentLeg, Journal

PARENT_ID = "browser-broker-parent"
CLIENT_ID = "browser-ingested-order"
SHORT_ID = "browser-broker-short-leg"
LONG_ID = "browser-broker-long-leg"


def broker_order() -> dict[str, object]:
    return {
        "id": PARENT_ID,
        "client_order_id": CLIENT_ID,
        "symbol": "XLE",
        "order_class": "mleg",
        "status": "filled",
        "qty": "3",
        "filled_qty": "3",
        "filled_avg_price": "-1.20",
        "filled_at": "2026-08-28T18:27:01.345678Z",
        "legs": [
            {
                "id": SHORT_ID,
                "symbol": SHORT_LEG,
                "side": "sell",
                "ratio_qty": "1",
                "position_intent": "sell_to_open",
                "status": "filled",
                "qty": "3",
                "filled_qty": "3",
                "filled_avg_price": "1.30",
            },
            {
                "id": LONG_ID,
                "symbol": LONG_LEG,
                "side": "buy",
                "ratio_qty": "1",
                "position_intent": "buy_to_open",
                "status": "filled",
                "qty": "3",
                "filled_qty": "3",
                "filled_avg_price": "0.10",
            },
        ],
    }


def broker_activities() -> list[dict[str, object]]:
    return [
        {
            "activity_type": "FILL",
            "id": "browser-raw-short-partial",
            "order_id": SHORT_ID,
            "symbol": SHORT_LEG,
            "side": "sell_short",
            "qty": "1",
            "price": "1.40",
            "cum_qty": "1",
            "leaves_qty": "2",
            "type": "partial_fill",
            "transaction_time": "2026-08-28T18:26:00.123456Z",
        },
        {
            "activity_type": "FILL",
            "id": "browser-raw-short-final",
            "order_id": SHORT_ID,
            "symbol": SHORT_LEG,
            "side": "sell_short",
            "qty": "2",
            "price": "1.25",
            "cum_qty": "3",
            "leaves_qty": "0",
            "type": "fill",
            "transaction_time": "2026-08-28T18:27:00.234567Z",
        },
        {
            "activity_type": "FILL",
            "id": "browser-raw-long-final",
            "order_id": LONG_ID,
            "symbol": LONG_LEG,
            "side": "buy",
            "qty": "3",
            "price": "0.10",
            "cum_qty": "3",
            "leaves_qty": "0",
            "type": "fill",
            "transaction_time": "2026-08-28T18:27:01.345678Z",
        },
    ]


@dataclass
class BrokerReadFixture:
    """Only GET is implemented, so the fixture cannot submit or cancel an order."""

    mode: str = "complete"

    def get(self, path: str, data: dict[str, str] | None = None) -> object:
        order = broker_order()
        if path == "/orders/" + PARENT_ID:
            return order
        legs = order["legs"]
        assert isinstance(legs, list)
        for leg in legs:
            assert isinstance(leg, dict)
            if path == "/orders/" + str(leg["id"]):
                return deepcopy(leg)
        if path == "/account/activities":
            if data and data.get("page_token"):
                return []
            rows = broker_activities()
            if self.mode == "pending":
                rows = rows[:1]
            elif self.mode == "conflict":
                rows[-1]["symbol"] = SHORT_LEG
            return rows
        raise AssertionError(f"Unexpected broker GET: {path}")


def populate_execution(path: Path, mode: str) -> None:
    """Use the exact production ingestion path, including reads and atomic persistence."""
    with Journal(path) as journal:
        journal.record_intent(
            client_order_id=CLIENT_ID,
            cycle_id="browser-ingestion",
            symbol="XLE",
            spreads=3,
            payload={"limit_price": "-1.20", "qty": "3", "type": "limit"},
            legs=(
                IntentLeg(SHORT_LEG, "sell", 1, "sell_to_open"),
                IntentLeg(LONG_LEG, "buy", 1, "buy_to_open"),
            ),
            at=NOW - timedelta(minutes=10),
        )
        journal.mark_submitted(CLIENT_ID, broker_order_id=PARENT_ID, at=NOW - timedelta(minutes=9))
        ingestor = ExecutionIngestor(journal, ActivityReader(BrokerReadFixture(mode)))
        result = ingestor.sync(NOW)
        assert result.summary["status"] == mode
        if mode == "complete":
            assert result.complete
            assert len(journal.execution_leg_rows(CLIENT_ID)) == 3
            assert len(journal.spread_fills_for(CLIENT_ID)) == 1
            # The same production delivery is idempotent before and after a restart.
            assert ingestor.sync(NOW).complete
            assert len(journal.execution_leg_rows(CLIENT_ID)) == 3
            assert len(journal.spread_fills_for(CLIENT_ID)) == 1
    if mode == "complete":
        with Journal(path) as reopened:
            assert (
                ExecutionIngestor(reopened, ActivityReader(BrokerReadFixture(mode)))
                .sync(NOW)
                .complete
            )
            assert len(reopened.execution_leg_rows(CLIENT_ID)) == 3
            assert len(reopened.spread_fills_for(CLIENT_ID)) == 1
