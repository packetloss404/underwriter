"""Real GET-shaped ingestion through planning, durable apply and recovery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.browser.execution_fixtures import (
    CLIENT_ID,
    PARENT_ID,
    broker_activities,
    broker_order,
)
from tests.test_dashboard import LONG_LEG, NOW, SHORT_LEG
from underwriter.execution_events import ActivityReader, ExecutionReadError
from underwriter.execution_ingestion import ExecutionIngestor, apply_execution_plan
from underwriter.journal import IntentLeg, Journal, JournalError, OrderStatus


@dataclass
class ReadApi:
    activities: list[dict[str, object]] = field(default_factory=broker_activities)
    orders: dict[str, dict[str, object]] = field(
        default_factory=lambda: {PARENT_ID: broker_order()}
    )
    reads: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(self, path: str, data: dict[str, str] | None = None) -> object:
        query = data or {}
        self.reads.append((path, dict(query)))
        if path == "/account/activities":
            start = 0
            if "page_token" in query:
                ids = [str(row["id"]) for row in self.activities]
                start = ids.index(query["page_token"]) + 1
            count = int(query.get("page_size", "100"))
            return deepcopy(self.activities[start : start + count])
        if path.startswith("/orders/"):
            return deepcopy(self.orders[path.removeprefix("/orders/")])
        pytest.fail("Only expected broker GETs are permitted")


def seed(journal: Journal, *, suffix: str = "", submitted: bool = True) -> None:
    journal.record_intent(
        client_order_id=CLIENT_ID + suffix,
        cycle_id="execution-integration",
        symbol="XLE",
        spreads=3,
        payload={"limit_price": "-1.20"},
        legs=(
            IntentLeg(SHORT_LEG, "sell", 1, "sell_to_open"),
            IntentLeg(LONG_LEG, "buy", 1, "buy_to_open"),
        ),
        at=NOW - timedelta(minutes=10),
    )
    if submitted:
        journal.mark_submitted(
            CLIENT_ID + suffix, broker_order_id=PARENT_ID + suffix, at=NOW - timedelta(minutes=9)
        )


def test_read_only_plan_actual_partial_leg_rows_and_restart_replay(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    api = ReadApi()
    with Journal(path) as journal:
        seed(journal)
        ingestor = ExecutionIngestor(journal, ActivityReader(api))
        plan = ingestor.plan(NOW)
        assert plan.complete and plan.summary["leg_events"] == 3
        assert journal.execution_leg_rows() == ()
        assert journal.spread_fills_for(CLIENT_ID) == ()
        assert journal.execution_ingestion_status()["status"] == "never"
        apply_execution_plan(journal, plan, at=NOW)
        (group,) = journal.spread_fills_for(CLIENT_ID)
        raw = journal.execution_leg_rows(CLIENT_ID)
        assert len(raw) == 3
        assert group.spreads == 3
        assert group.net_price_per_spread == pytest.approx(-1.2)
        assert group.fill_id in {row.fill_id for row in raw}
        assert group.occurred_at == max(row.occurred_at for row in raw)
        assert sum(row.contracts for row in raw if row.side == "sell") == 3
        assert {row.parent_fill_id for row in raw} == {group.fill_id}
        assert journal.order(CLIENT_ID).status is OrderStatus.FILLED  # type: ignore[union-attr]
        assert ingestor.sync(NOW).summary["leg_events"] == 0
    with Journal(path) as journal:
        result = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert result.complete
        assert result.summary["leg_events"] == result.summary["derived_groups"] == 0
        assert len(journal.execution_leg_rows()) == 3
        assert len(journal.spread_fills_for(CLIENT_ID)) == 1
        assert journal.execution_ingestion_status()["status"] == "complete"
        assert "next_page_token" not in journal.execution_ingestion_status()


def test_partial_subset_with_different_average_is_pending_and_retained() -> None:
    with Journal() as journal:
        seed(journal)
        api = ReadApi(activities=broker_activities()[:1])
        result = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert result.summary["status"] == "pending"
        assert len(journal.execution_leg_rows()) == 1
        assert journal.spread_fills_for(CLIENT_ID) == ()
        assert "price_conflict" not in str(result.summary["issues"])
        api.activities = broker_activities()
        final = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert final.complete
        assert len(journal.execution_leg_rows()) == 3
        assert len(journal.spread_fills_for(CLIENT_ID)) == 1


def test_bounded_pagination_restarts_until_empty_terminal_page(tmp_path: Path) -> None:
    path, api = tmp_path / "paged.db", ReadApi()
    with Journal(path) as journal:
        seed(journal)
    statuses = []
    for _ in range(4):
        with Journal(path) as journal:
            result = ExecutionIngestor(journal, ActivityReader(api), max_pages=1, page_size=1).sync(
                NOW
            )
            statuses.append(result.summary["status"])
    assert statuses == ["pending", "pending", "pending", "complete"]
    with Journal(path) as journal:
        assert len(journal.execution_leg_rows()) == 3
        assert len(journal.spread_fills_for(CLIENT_ID)) == 1


def test_unmapped_first_page_is_never_skipped_by_a_checkpoint() -> None:
    unknown = deepcopy(broker_activities()[0])
    unknown["id"], unknown["order_id"] = "unknown-real-activity", "unknown-real-child"
    api = ReadApi(activities=[unknown, *broker_activities()])
    with Journal() as journal:
        seed(journal)
        for _ in range(3):
            result = ExecutionIngestor(journal, ActivityReader(api), max_pages=1, page_size=1).sync(
                NOW
            )
            assert result.summary["status"] == "pending"
            assert result.summary["next_page_token"] is None
        assert journal.execution_leg_rows() == ()
        assert all(
            "page_token" not in data for path, data in api.reads if path == "/account/activities"
        )


def test_conflict_page_does_not_advance_past_unapplied_valid_rows() -> None:
    rows = broker_activities()
    rows[0]["symbol"] = LONG_LEG
    api = ReadApi(activities=rows)
    with Journal() as journal:
        seed(journal)
        for _ in range(2):
            result = ExecutionIngestor(journal, ActivityReader(api), max_pages=1, page_size=1).sync(
                NOW
            )
            assert result.summary["status"] == "conflict"
            assert result.summary["next_page_token"] is None
        assert journal.execution_leg_rows() == ()


def test_leg_price_disagreement_cannot_cancel_inside_parent_net_price() -> None:
    api = ReadApi()
    for row in api.activities:
        row["price"] = str(float(str(row["price"])) + 0.1)
    with Journal() as journal:
        seed(journal)
        plan = ExecutionIngestor(journal, ActivityReader(api)).plan(NOW)
        assert plan.summary["status"] == "conflict"
        assert "leg_price_conflict" in str(plan.summary["issues"])
        assert journal.execution_leg_rows() == ()


def test_child_position_intent_must_equal_the_journal_intent() -> None:
    api = ReadApi()
    children = api.orders[PARENT_ID]["legs"]
    assert isinstance(children, list)
    children[0]["position_intent"] = "sell_to_close"
    with Journal() as journal:
        seed(journal)
        with pytest.raises(ExecutionReadError, match="side or ratio"):
            ExecutionIngestor(journal, ActivityReader(api)).plan(NOW)
        assert journal.execution_leg_rows() == ()


def test_atomic_apply_rolls_back_raw_rows_and_parent_status_on_price_conflict() -> None:
    with Journal() as journal:
        seed(journal)
        plan = ExecutionIngestor(journal, ActivityReader(ReadApi())).plan(NOW)
        damaged = replace(plan, groups=(replace(plan.groups[0], net_price_per_spread=-1.1),))
        with pytest.raises(JournalError, match="price"):
            apply_execution_plan(journal, damaged, at=NOW)
        assert journal.execution_leg_rows() == ()
        assert journal.spread_fills_for(CLIENT_ID) == ()
        parent = journal.order(CLIENT_ID)
        assert parent is not None and parent.status is OrderStatus.SUBMITTED
        assert journal.execution_ingestion_status()["status"] == "never"


def test_zero_premium_is_explicit_pending_and_cursor_cannot_skip_it() -> None:
    rows = broker_activities()
    rows[0]["price"] = "0"
    api = ReadApi(activities=rows)
    with Journal() as journal:
        seed(journal)
        result = ExecutionIngestor(journal, ActivityReader(api), max_pages=1, page_size=1).sync(NOW)
        assert result.summary["status"] == "pending"
        assert result.summary["next_page_token"] is None
        assert "zero_premium_unsupported" in str(result.summary["issues"])
        assert journal.execution_leg_rows() == ()


def test_all_parents_are_reconciled_beyond_recent_history_limit() -> None:
    api = ReadApi(activities=[], orders={})
    with Journal() as journal:
        for index in range(105):
            suffix = f"-{index}"
            seed(journal, suffix=suffix)
            parent = broker_order()
            parent["id"], parent["client_order_id"] = PARENT_ID + suffix, CLIENT_ID + suffix
            children = parent["legs"]
            assert isinstance(children, list)
            for child in children:
                child["id"] = str(child["id"]) + suffix
            api.orders[PARENT_ID + suffix] = parent
            for row in broker_activities():
                row["id"], row["order_id"] = str(row["id"]) + suffix, str(row["order_id"]) + suffix
                api.activities.append(row)
        result = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW, all_orders=True)
        assert result.complete
        assert result.summary["orders_reconciled"] == 105
        assert result.summary["derived_groups"] == 105
        assert result.summary["leg_events"] == 315
        assert len(journal.execution_leg_rows()) == 315


def test_later_partial_activities_with_older_ids_keep_the_real_anchor() -> None:
    parent = broker_order()
    parent["status"], parent["filled_qty"] = "partially_filled", "1"
    children = parent["legs"]
    assert isinstance(children, list)
    for child in children:
        child["filled_qty"] = "1"
    first = [deepcopy(broker_activities()[0]), deepcopy(broker_activities()[2])]
    first[0].update(id="z-short", qty="1", price="1.30", cum_qty="1", leaves_qty="2")
    first[1].update(id="z-long", qty="1", cum_qty="1", leaves_qty="2")
    api = ReadApi(activities=first, orders={PARENT_ID: parent})
    with Journal() as journal:
        seed(journal)
        initial = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert initial.complete
        (original,) = journal.spread_fills_for(CLIENT_ID)
        assert original.spreads == 1
        later = [deepcopy(first[0]), deepcopy(first[1])]
        for index, row in enumerate(later):
            row.update(
                id="a-short" if index == 0 else "a-long",
                qty="2",
                cum_qty="3",
                leaves_qty="0",
                type="fill",
                transaction_time="2026-08-28T18:29:00.123456Z",
            )
        api.activities = [*later, *first]
        api.orders[PARENT_ID] = broker_order()
        final = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert final.complete
        (updated,) = journal.spread_fills_for(CLIENT_ID)
        assert updated.fill_id == original.fill_id
        assert updated.spreads == 3
        assert final.summary["derived_groups"] == 0
        assert final.summary["updated_groups"] == 1
        assert len(journal.execution_leg_rows()) == 4


def test_changed_activity_id_is_not_overwritten_on_replay() -> None:
    api = ReadApi()
    with Journal() as journal:
        seed(journal)
        ingestor = ExecutionIngestor(journal, ActivityReader(api))
        assert ingestor.sync(NOW).complete
        before = journal.execution_leg_rows()
        api.activities[0]["price"] = "1.41"
        result = ingestor.sync(NOW)
        assert result.summary["status"] == "conflict"
        assert journal.execution_leg_rows() == before
        assert journal.execution_ingestion_status()["status"] == "conflict"


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.ABANDONED,
        OrderStatus.INTENT,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    ],
)
def test_only_proven_unsubmitted_zero_fill_intents_need_no_broker_identity(
    status: OrderStatus,
) -> None:
    with Journal() as journal:
        seed(journal, submitted=False)
        if status is OrderStatus.ABANDONED:
            journal.abandon(CLIENT_ID, detail="GET proved the intent never reached broker", at=NOW)
        elif status is not OrderStatus.INTENT:
            journal.mark_status(CLIENT_ID, status, at=NOW)
        result = ExecutionIngestor(journal, ActivityReader(ReadApi(activities=[], orders={}))).sync(
            NOW, all_orders=True
        )
        assert result.complete is (status is OrderStatus.ABANDONED)
        if status is not OrderStatus.ABANDONED:
            assert "parent_identity_pending" in str(result.summary["issues"])
        assert journal.execution_leg_rows() == ()


@pytest.mark.parametrize("status", ["filled", "partially_filled"])
def test_known_filled_broker_status_with_zero_quantity_is_pending(status: str) -> None:
    api = ReadApi(activities=[])
    parent = api.orders[PARENT_ID]
    parent.update(status=status, filled_qty="0", filled_avg_price=None)
    children = parent["legs"]
    assert isinstance(children, list)
    for child in children:
        child.update(filled_qty="0", filled_avg_price=None)
    with Journal() as journal:
        seed(journal)
        result = ExecutionIngestor(journal, ActivityReader(api)).sync(NOW)
        assert result.summary["status"] == "pending"
        assert result.summary["orders_reconciled"] == 0
        assert "parent_quantity_pending" in str(result.summary["issues"])
        assert journal.spread_fills_for(CLIENT_ID) == ()
