"""Journal tests.

Weighted towards crashes, duplicates and reopens, because those are the only
conditions under which this module earns its existence. A journal that works
when nothing goes wrong is a log file.

Three questions recur. Does an unknown read as an unknown -- losing an order
intent is the obvious failure, but quietly returning 0.0 for a realised P&L
nobody recorded is the dangerous one, because it disarms the daily loss stop
while looking healthy. Does a half-known thing read as known -- a fill heard
on a socket that reconnects without a cursor is a rumour until REST agrees.
And are spreads ever confused with contracts, or a signed net price with a leg
premium, because that mistake misstates open risk without ever looking wrong.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from underwriter.journal import (
    SCHEMA_VERSION,
    ConflictingIntentError,
    FillAttribution,
    FillSource,
    IntentLeg,
    Journal,
    JournalError,
    KillSwitchActor,
    OrderStatus,
    PnlSource,
    PositionEventCause,
    PositionEventEvidence,
    PositionRecord,
    ReconciliationScope,
    RecoveryGap,
    SchemaTooNewError,
    SchemaTooOldError,
    Stage,
    UnitConfusionError,
    UnknownOrderError,
    realised_pnl_from_fills,
    spread_realised_pnl,
    trading_day_of,
)

DAY = date(2026, 9, 2)
OPEN_ET = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)  # 09:30 ET
EQUITY = 100_000.0
SPREADS = 5.0
# A filled credit's net price is negative: the money moves towards us.
CREDIT = -1.20
DEBIT = 0.40
PAYLOAD: dict[str, object] = {
    "order_class": "mleg",
    "type": "limit",
    # Negative because a credit spread's net limit price is signed.
    # See docs/GOTCHAS.md #7.
    "limit_price": "-1.20",
    "legs": [
        {"symbol": "XLE260911P00082000", "side": "sell", "ratio_qty": "1"},
        {"symbol": "XLE260911P00080000", "side": "buy", "ratio_qty": "1"},
    ],
}
# Buying the spread back is a debit, so the sign flips positive on the close.
CLOSING_PAYLOAD: dict[str, object] = {**PAYLOAD, "limit_price": "0.40"}
SHORT_LEG = "XLE260911P00082000"
LONG_LEG = "XLE260911P00080000"
LEGS: tuple[IntentLeg, ...] = (
    IntentLeg(SHORT_LEG, "sell", 1, "sell_to_open"),
    IntentLeg(LONG_LEG, "buy", 1, "buy_to_open"),
)


@pytest.fixture
def journal() -> Journal:
    return Journal()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "journal.sqlite3"


def at(minutes: float = 0) -> datetime:
    """A deterministic timestamp `minutes` into the session."""
    return OPEN_ET + timedelta(minutes=minutes)


def intent(
    j: Journal,
    *,
    client_order_id: str = "uw-1",
    symbol: str = "XLE",
    cycle_id: str = "cycle-1",
    spreads: float = SPREADS,
    payload: dict[str, object] | None = None,
    legs: Sequence[IntentLeg] = LEGS,
    minutes: float = 0,
) -> None:
    j.record_intent(
        client_order_id=client_order_id,
        cycle_id=cycle_id,
        symbol=symbol,
        spreads=spreads,
        payload=PAYLOAD if payload is None else payload,
        legs=legs,
        at=at(minutes),
    )


def fill(
    j: Journal,
    *,
    fill_id: str = "exec-1",
    symbol: str = "XLE",
    spreads: float = SPREADS,
    net_price_per_spread: float = CREDIT,
    source: FillSource = FillSource.REST,
    client_order_id: str | None = "uw-1",
    broker_order_id: str | None = None,
    minutes: float = 1,
) -> bool:
    return j.record_spread_fill(
        fill_id=fill_id,
        symbol=symbol,
        spreads=spreads,
        net_price_per_spread=net_price_per_spread,
        occurred_at=at(minutes),
        source=source,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        at=at(minutes),
    )


def settled(j: Journal, *, minutes: float = 5) -> None:
    """Bring a journal to a state where recovery has nothing to complain about."""
    j.record_session_open_equity(equity=EQUITY, at=at())
    j.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0.0, at=at(minutes))
    j.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at(minutes))


class TestOpening:
    def test_memory_database_starts_empty(self, journal: Journal) -> None:
        assert journal.order_history() == ()
        assert journal.recent_decisions() == ()
        assert journal.latest_positions().observed is False

    def test_file_database_uses_wal(self, db_path: Path) -> None:
        # The durability promise is WAL plus synchronous=FULL. If the file
        # would not enter WAL we would be running on a guarantee we do not
        # have, so the mode is asserted rather than assumed.
        with Journal(db_path) as j:
            assert j.journal_mode == "wal"

    def test_reopening_preserves_everything(self, db_path: Path) -> None:
        with Journal(db_path) as j:
            intent(j)
            j.record_session_open_equity(equity=EQUITY, at=at())

        with Journal(db_path) as j:
            stored = j.order("uw-1")
            assert stored is not None
            assert stored.payload == PAYLOAD
            assert stored.spreads_ordered == pytest.approx(SPREADS)
            assert j.session_open_equity(DAY) == pytest.approx(EQUITY)

    def test_newer_schema_is_refused(self, db_path: Path) -> None:
        # A column this build cannot see could be the one holding an open
        # position. Reading around it would look like a healthy empty book.
        Journal(db_path).close()
        with sqlite3.connect(db_path) as raw:
            raw.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION + 1, at().isoformat()),
            )
        with pytest.raises(SchemaTooNewError, match="Refusing to open"):
            Journal(db_path)

    def test_older_schema_is_refused_rather_than_guessed_at(self, db_path: Path) -> None:
        Journal(db_path).close()
        with sqlite3.connect(db_path) as raw:
            raw.execute("DELETE FROM schema_version")
            raw.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION - 1, at().isoformat()),
            )
        with pytest.raises(SchemaTooOldError, match=r"no .*migration"):
            Journal(db_path)

    def test_empty_version_table_is_refused(self, db_path: Path) -> None:
        Journal(db_path).close()
        with sqlite3.connect(db_path) as raw:
            raw.execute("DELETE FROM schema_version")
        with pytest.raises(JournalError, match="empty schema_version"):
            Journal(db_path)

    def test_someone_elses_database_is_refused(self, db_path: Path) -> None:
        # Creating our tables alongside theirs would half-work, which is worse
        # than not working.
        with sqlite3.connect(db_path) as raw:
            raw.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
        with pytest.raises(JournalError, match="not an underwriter journal"):
            Journal(db_path)


class TestDecisions:
    def test_reasons_and_detail_survive_the_round_trip(self, journal: Journal) -> None:
        journal.record_decision(
            cycle_id="cycle-1",
            stage=Stage.RISK,
            accepted=False,
            symbol="XLE",
            reasons=["daily_loss_stop", "correlated_exposure"],
            detail=["Day P&L -1,600 breaches the 1.5% stop."],
            context={"equity": EQUITY},
            at=at(5),
        )
        (record,) = journal.recent_decisions()
        assert record.stage is Stage.RISK
        assert record.reasons == ("daily_loss_stop", "correlated_exposure")
        assert record.detail == ("Day P&L -1,600 breaches the 1.5% stop.",)
        assert record.context == {"equity": EQUITY}
        assert record.at == at(5)

    def test_a_rejection_without_a_reason_is_refused(self, journal: Journal) -> None:
        # The submission promises a judge that every refusal names itself.
        with pytest.raises(ValueError, match="at least one reason"):
            journal.record_decision(
                cycle_id="cycle-1", stage=Stage.VETO, accepted=False, symbol="XLE"
            )

    def test_an_acceptance_needs_no_reason(self, journal: Journal) -> None:
        journal.record_decision(cycle_id="cycle-1", stage=Stage.RISK, accepted=True, symbol="XLE")
        assert len(journal.recent_decisions()) == 1

    def test_rejections_excludes_accepted_candidates(self, journal: Journal) -> None:
        journal.record_decision(
            cycle_id="c", stage=Stage.RISK, accepted=True, symbol="XLE", at=at(1)
        )
        journal.record_decision(
            cycle_id="c",
            stage=Stage.RISK,
            accepted=False,
            symbol="XLF",
            reasons=["position_cap"],
            at=at(2),
        )
        assert [r.symbol for r in journal.rejections()] == ["XLF"]

    def test_recent_decisions_are_newest_first(self, journal: Journal) -> None:
        for minute, symbol in enumerate(("XLE", "XLF", "XLV")):
            journal.record_decision(
                cycle_id="c", stage=Stage.RANK, accepted=True, symbol=symbol, at=at(minute)
            )
        assert [r.symbol for r in journal.recent_decisions()] == ["XLV", "XLF", "XLE"]
        assert [r.symbol for r in journal.recent_decisions(limit=1)] == ["XLV"]

    def test_decisions_filter_by_cycle_and_symbol(self, journal: Journal) -> None:
        journal.record_decision(
            cycle_id="c1", stage=Stage.RANK, accepted=True, symbol="XLE", at=at(1)
        )
        journal.record_decision(
            cycle_id="c2", stage=Stage.RANK, accepted=True, symbol="XLE", at=at(2)
        )
        journal.record_decision(
            cycle_id="c2", stage=Stage.RANK, accepted=True, symbol="XLF", at=at(3)
        )
        assert len(journal.recent_decisions(cycle_id="c2")) == 2
        assert len(journal.recent_decisions(symbol="XLE")) == 2
        assert len(journal.recent_decisions(cycle_id="c2", symbol="XLF")) == 1


class TestTimestamps:
    def test_naive_timestamps_are_refused(self, journal: Journal) -> None:
        # Assuming a naive timestamp is UTC is right until the one time it is
        # not, and by then it is in the audit trail.
        naive = datetime(2026, 9, 2, 13, 30)  # noqa: DTZ001 -- the point of the test
        with pytest.raises(ValueError, match="timezone-aware"):
            journal.record_decision(
                cycle_id="c", stage=Stage.SCAN, accepted=True, symbol="XLE", at=naive
            )

    def test_non_utc_timestamps_come_back_as_utc(self, journal: Journal) -> None:
        eastern = timezone(timedelta(hours=-4))
        journal.record_decision(
            cycle_id="c",
            stage=Stage.SCAN,
            accepted=True,
            symbol="XLE",
            at=datetime(2026, 9, 2, 9, 30, tzinfo=eastern),
        )
        (record,) = journal.recent_decisions()
        assert record.at.tzinfo is UTC
        assert record.at == OPEN_ET

    def test_view_age_refuses_a_naive_now(self, journal: Journal) -> None:
        journal.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at())
        with pytest.raises(ValueError, match="timezone-aware"):
            journal.view_age(now=datetime(2026, 9, 2, 14, 0))  # noqa: DTZ001


class TestWriteAheadOfSubmission:
    def test_an_intent_is_durable_before_anything_is_submitted(self, journal: Journal) -> None:
        stored = journal.record_intent(
            client_order_id="uw-1",
            cycle_id="cycle-1",
            symbol="XLE",
            spreads=SPREADS,
            payload=PAYLOAD,
            legs=LEGS,
            at=at(),
        )
        assert stored.status is OrderStatus.INTENT
        assert stored.submitted_at is None
        # Never confirmed against the broker: this is exactly the state a crash
        # between journalling and submitting leaves behind.
        assert stored.reconciled_at is None
        assert stored.needs_reconciliation
        assert stored.spreads_working == pytest.approx(SPREADS)

    def test_a_crash_between_intent_and_submission_is_recoverable(self, db_path: Path) -> None:
        # Simulate the crash by writing the intent and never submitting: the
        # process simply goes away with the connection open.
        crashed = Journal(db_path)
        intent(crashed, client_order_id="uw-1")
        crashed.close()

        with Journal(db_path) as restarted:
            (pending,) = restarted.unreconciled_orders()
            assert pending.client_order_id == "uw-1"
            assert pending.status is OrderStatus.INTENT
            # The payload is what a retry decision needs, and it survived.
            assert pending.payload == PAYLOAD

    def test_a_settled_order_is_not_chased_again(self, journal: Journal) -> None:
        intent(journal)
        journal.mark_submitted("uw-1", broker_order_id="b-1", at=at(1))
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=SPREADS, at=at(2))
        assert journal.unreconciled_orders() == ()

    def test_a_working_order_is_chased_on_every_restart(self, db_path: Path) -> None:
        # Having been seen once is not the same as being finished. An order the
        # broker last called "accepted" is still live and must come back on the
        # next restart too.
        with Journal(db_path) as j:
            intent(j)
            j.mark_status("uw-1", OrderStatus.ACCEPTED, broker_order_id="b-1", at=at(1))

        with Journal(db_path) as j:
            (pending,) = j.unreconciled_orders()
            assert pending.status is OrderStatus.ACCEPTED
            assert pending.reconciled_at == at(1)

    def test_status_for_an_unjournalled_order_is_loud(self, journal: Journal) -> None:
        # Under the write-ahead rule this cannot happen for an order we placed,
        # so it means the rule was broken and the quiet fix would hide it.
        # Fills are deliberately more forgiving; see TestFillAttribution.
        with pytest.raises(UnknownOrderError, match="write-ahead rule"):
            journal.mark_submitted("never-journalled", at=at(1))

    def test_a_settled_order_cannot_change_its_fate(self, journal: Journal) -> None:
        intent(journal)
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=SPREADS, at=at(1))
        with pytest.raises(JournalError, match="already filled"):
            journal.mark_status("uw-1", OrderStatus.CANCELLED, at=at(2))

    def test_a_repeated_terminal_status_is_idempotent(self, journal: Journal) -> None:
        # The stream and the reconciling poll can both deliver the same fill.
        intent(journal)
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=SPREADS, at=at(1))
        again = journal.mark_status("uw-1", OrderStatus.FILLED, at=at(2))
        assert again.status is OrderStatus.FILLED
        assert again.spreads_filled == pytest.approx(SPREADS)

    def test_abandoning_an_intent_requires_a_reason(self, journal: Journal) -> None:
        intent(journal)
        with pytest.raises(ValueError, match="requires a reason"):
            journal.abandon("uw-1", detail="  ", at=at(1))

    def test_abandoned_intents_settle_with_their_explanation(self, journal: Journal) -> None:
        intent(journal)
        record = journal.abandon(
            "uw-1", detail="Broker had no order with this client_order_id.", at=at(1)
        )
        assert record.status is OrderStatus.ABANDONED
        assert not record.needs_reconciliation
        assert "no order" in record.detail

    def test_order_history_is_newest_intent_first(self, journal: Journal) -> None:
        intent(journal, client_order_id="uw-1", minutes=0)
        intent(journal, client_order_id="uw-2", symbol="XLF", minutes=5)
        assert [o.client_order_id for o in journal.order_history()] == ["uw-2", "uw-1"]


class TestIdempotentIntents:
    def test_the_same_intent_twice_creates_one_order(self, journal: Journal) -> None:
        first = journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=SPREADS,
            payload=PAYLOAD,
            legs=LEGS,
            at=at(),
        )
        second = journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=SPREADS,
            payload=PAYLOAD,
            legs=LEGS,
            at=at(3),
        )
        assert first == second
        assert len(journal.order_history()) == 1
        # The original intent time is what reconciliation reasons about, so the
        # retry must not overwrite it.
        assert second.intent_at == at()

    def test_key_order_in_the_payload_does_not_make_it_a_new_order(self, journal: Journal) -> None:
        for minute in (0, 1):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=SPREADS,
                payload={"a": 1, "b": 2} if minute == 0 else {"b": 2, "a": 1},
                legs=LEGS,
                at=at(minute),
            )
        assert len(journal.order_history()) == 1

    def test_reusing_an_id_for_a_different_order_is_refused(self, journal: Journal) -> None:
        # The id is what reconciliation matches on, and Alpaca does not
        # de-duplicate it (docs/GOTCHAS.md #9). Two different orders behind one
        # id would make the crash unrecoverable.
        intent(journal)
        with pytest.raises(ConflictingIntentError, match="different payload"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=SPREADS,
                payload={**PAYLOAD, "limit_price": "-0.40"},
                legs=LEGS,
                at=at(1),
            )

    def test_reusing_an_id_for_a_different_size_is_refused(self, journal: Journal) -> None:
        intent(journal, spreads=5)
        with pytest.raises(ConflictingIntentError):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=3,
                payload=PAYLOAD,
                legs=LEGS,
                at=at(1),
            )

    def test_reusing_an_id_for_a_different_symbol_is_refused(self, journal: Journal) -> None:
        intent(journal, symbol="XLE")
        with pytest.raises(ConflictingIntentError):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLF",
                spreads=SPREADS,
                payload=PAYLOAD,
                legs=LEGS,
                at=at(1),
            )

    def test_an_empty_client_order_id_is_refused(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            journal.record_intent(
                client_order_id="   ",
                cycle_id="c",
                symbol="XLE",
                spreads=SPREADS,
                payload=PAYLOAD,
                legs=LEGS,
                at=at(),
            )

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
    def test_an_order_for_no_spreads_is_refused(self, journal: Journal, bad: float) -> None:
        with pytest.raises(ValueError, match="spread"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=bad,
                payload=PAYLOAD,
                legs=LEGS,
                at=at(),
            )


class TestBrokerStatusMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("new", OrderStatus.ACCEPTED),
            ("PARTIALLY_FILLED", OrderStatus.PARTIALLY_FILLED),
            ("filled", OrderStatus.FILLED),
            ("canceled", OrderStatus.CANCELLED),
            ("rejected", OrderStatus.REJECTED),
        ],
    )
    def test_known_statuses_map_through(self, raw: str, expected: OrderStatus) -> None:
        assert OrderStatus.from_broker(raw) is expected

    @pytest.mark.parametrize("raw", ["replaced", "done_for_day", "stopped", "wat"])
    def test_ambiguous_statuses_stay_unknown_and_unsettled(self, raw: str) -> None:
        # An ambiguous status says nothing about whether anything filled.
        # Resolving it to "probably nothing" is how a book acquires a position
        # nobody is watching, so it maps to UNKNOWN, which is non-terminal.
        status = OrderStatus.from_broker(raw)
        assert status is OrderStatus.UNKNOWN
        assert not status.is_terminal


class TestPartialParentFills:
    def test_a_partially_filled_parent_keeps_both_numbers(self, journal: Journal) -> None:
        # Legs fill together, but two of five spreads filling while three keep
        # working is a balanced, smaller position -- neither "filled" nor
        # "unfilled" (docs/GOTCHAS.md #8).
        intent(journal, spreads=5)
        order = journal.mark_status(
            "uw-1",
            OrderStatus.PARTIALLY_FILLED,
            spreads_filled=2,
            net_price_per_spread=CREDIT,
            at=at(1),
        )
        assert order.spreads_ordered == pytest.approx(5.0)
        assert order.spreads_filled == pytest.approx(2.0)
        assert order.spreads_working == pytest.approx(3.0)
        assert order.is_partially_filled
        assert order.net_price_per_spread == pytest.approx(CREDIT)

    def test_a_partially_filled_parent_is_still_chased(self, journal: Journal) -> None:
        intent(journal, spreads=5)
        journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, spreads_filled=2, at=at(1))
        (pending,) = journal.unreconciled_orders()
        assert pending.spreads_working == pytest.approx(3.0)

    def test_a_fully_filled_parent_is_not_partial(self, journal: Journal) -> None:
        intent(journal, spreads=5)
        order = journal.mark_status(
            "uw-1", OrderStatus.FILLED, spreads_filled=5, net_price_per_spread=CREDIT, at=at(1)
        )
        assert not order.is_partially_filled
        assert order.spreads_working == pytest.approx(0.0)

    def test_contracts_written_where_spreads_belong_is_refused(self, journal: Journal) -> None:
        # Five spreads across two legs is ten contracts. Ten filled against
        # five ordered is impossible, and storing it would double the book's
        # apparent size for a reason nobody would find.
        intent(journal, spreads=5)
        with pytest.raises(UnitConfusionError, match="contract count"):
            journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, spreads_filled=10, at=at(1))

    def test_a_filled_quantity_cannot_go_backwards(self, journal: Journal) -> None:
        # Fills do not un-happen; a smaller number means we are reading a
        # different order.
        intent(journal, spreads=5)
        journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, spreads_filled=3, at=at(1))
        with pytest.raises(UnitConfusionError, match="do not un-happen"):
            journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, spreads_filled=1, at=at(2))

    def test_omitting_the_filled_quantity_leaves_it_alone(self, journal: Journal) -> None:
        # A status-only update (an acknowledgement, say) must not silently zero
        # the fill state it says nothing about.
        intent(journal, spreads=5)
        journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, spreads_filled=2, at=at(1))
        order = journal.mark_status("uw-1", OrderStatus.PARTIALLY_FILLED, at=at(2))
        assert order.spreads_filled == pytest.approx(2.0)


class TestUnitsAndSigns:
    def test_a_credit_spread_realises_the_difference(self) -> None:
        # Sold at 1.20, bought back at 0.40, one spread: 0.80 x 100 = 80.
        assert spread_realised_pnl(
            open_net_price=CREDIT, close_net_price=DEBIT, spreads=1
        ) == pytest.approx(80.0)

    def test_a_credit_spread_that_goes_wrong_loses(self) -> None:
        # Sold at 1.20, bought back at 1.80: a 0.60 loss per spread.
        assert spread_realised_pnl(
            open_net_price=CREDIT, close_net_price=1.80, spreads=2
        ) == pytest.approx(-120.0)

    def test_the_sign_convention_survives_a_debit_spread(self) -> None:
        # Paid 0.50 to open, sold at 0.90 to close: a 0.40 gain. A formula
        # written as `credit - debit` gets this one backwards.
        assert spread_realised_pnl(
            open_net_price=0.50, close_net_price=-0.90, spreads=1
        ) == pytest.approx(40.0)

    def test_realised_pnl_sums_a_round_trip_from_its_fills(self, journal: Journal) -> None:
        # A round trip is two orders: opening at a credit and closing at a
        # debit. One order cannot hold both, which is why the fill ledger
        # refuses to let its fills sum past what it ordered.
        intent(journal, spreads=2)
        intent(journal, client_order_id="uw-2", spreads=2, payload=CLOSING_PAYLOAD, minutes=59)
        fill(journal, fill_id="open", spreads=2, net_price_per_spread=CREDIT, minutes=1)
        fill(
            journal,
            fill_id="close",
            spreads=2,
            net_price_per_spread=DEBIT,
            client_order_id="uw-2",
            minutes=60,
        )
        round_trip = (*journal.spread_fills_for("uw-1"), *journal.spread_fills_for("uw-2"))
        assert realised_pnl_from_fills(round_trip) == pytest.approx(160.0)

    def test_an_opening_fill_alone_is_credit_received_not_profit(self, journal: Journal) -> None:
        # The helper reports the money in the account, which is not the same as
        # money earned. This is why today's realised P&L comes from the broker
        # rather than from summing fills.
        intent(journal, spreads=2)
        fill(journal, fill_id="open", spreads=2, net_price_per_spread=CREDIT, minutes=1)
        (stored,) = journal.spread_fills_for("uw-1")
        assert stored.credit_received == pytest.approx(240.0)
        assert realised_pnl_from_fills((stored,)) == pytest.approx(240.0)

    def test_a_leg_premium_must_be_positive(self, journal: Journal) -> None:
        # A leg's price is its own premium, positive on both sides. A negative
        # one is the parent's signed net written into a leg row, which would
        # flip the sign of the position's P&L and never look wrong.
        with pytest.raises(UnitConfusionError, match="always positive"):
            journal.record_leg_fill(
                fill_id="leg-1",
                occ_symbol="XLE260911P00082000",
                contracts=5,
                premium_per_contract=CREDIT,
                side="sell",
                occurred_at=at(1),
                source=FillSource.REST,
            )

    def test_leg_contracts_and_parent_spreads_are_different_numbers(self, journal: Journal) -> None:
        # One execution, two units: 5 spreads on the parent, 5 contracts on
        # each of two 1:1 legs. Storing them in one column would force a
        # choice that is wrong for one of the readers.
        intent(journal, spreads=5)
        fill(journal, fill_id="exec-1", spreads=5, net_price_per_spread=CREDIT)
        for index, (occ, side) in enumerate(
            (("XLE260911P00082000", "sell"), ("XLE260911P00080000", "buy"))
        ):
            journal.record_leg_fill(
                fill_id=f"leg-{index}",
                occ_symbol=occ,
                contracts=5,
                premium_per_contract=2.10 if side == "sell" else 0.90,
                side=side,
                occurred_at=at(1),
                source=FillSource.REST,
                parent_fill_id="exec-1",
                client_order_id="uw-1",
            )
        (parent,) = journal.spread_fills_for("uw-1")
        legs = journal.leg_fills_for("exec-1")
        assert parent.spreads == pytest.approx(5.0)
        assert parent.net_price_per_spread < 0
        assert [leg.contracts for leg in legs] == [pytest.approx(5.0), pytest.approx(5.0)]
        assert all(leg.premium_per_contract > 0 for leg in legs)

    def test_an_unrecognised_side_is_refused(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="side must be"):
            journal.record_leg_fill(
                fill_id="leg-1",
                occ_symbol="XLE260911P00082000",
                contracts=5,
                premium_per_contract=2.10,
                side="short",
                occurred_at=at(1),
                source=FillSource.REST,
            )

    def test_a_fill_cannot_exceed_what_its_order_asked_for(self, journal: Journal) -> None:
        # Five spreads across two legs is ten contracts. The order row already
        # refuses this; the fill ledger has to refuse it too, because that is
        # the column realised P&L is computed from.
        intent(journal, spreads=5)
        with pytest.raises(UnitConfusionError, match="contract count"):
            fill(journal, spreads=10, net_price_per_spread=CREDIT)

    def test_fills_cannot_accumulate_past_the_order(self, journal: Journal) -> None:
        intent(journal, spreads=5)
        fill(journal, fill_id="exec-1", spreads=3, minutes=1)
        fill(journal, fill_id="exec-2", spreads=2, minutes=2)
        with pytest.raises(UnitConfusionError, match="5 ordered"):
            fill(journal, fill_id="exec-3", spreads=1, minutes=3)

    def test_a_credit_order_cannot_fill_at_a_debit(self, journal: Journal) -> None:
        # The reviewer's case: a leg's positive premium written into the
        # parent's signed net. It would record the credit with the wrong sign
        # and show up only as mysteriously bad P&L.
        intent(journal, spreads=5)
        with pytest.raises(UnitConfusionError, match="cannot fill at a debit"):
            fill(journal, spreads=5, net_price_per_spread=1.20)

    def test_a_debit_order_cannot_fill_at_a_credit(self, journal: Journal) -> None:
        intent(journal, spreads=5, payload=CLOSING_PAYLOAD)
        with pytest.raises(UnitConfusionError):
            fill(journal, spreads=5, net_price_per_spread=CREDIT)

    def test_a_closing_debit_against_a_closing_order_is_fine(self, journal: Journal) -> None:
        intent(journal, spreads=5, payload=CLOSING_PAYLOAD)
        assert fill(journal, spreads=5, net_price_per_spread=DEBIT)

    def test_a_confirmation_is_checked_as_well_as_an_insert(self, journal: Journal) -> None:
        # The upgrade path writes REST's figures over the stream's, so it has
        # to be guarded too or the check is bypassed by arriving twice.
        intent(journal, spreads=5)
        fill(journal, spreads=5, net_price_per_spread=CREDIT, source=FillSource.STREAM)
        with pytest.raises(UnitConfusionError):
            fill(journal, spreads=10, net_price_per_spread=CREDIT, source=FillSource.REST)

    def test_a_repeat_stream_delivery_is_not_validated(self, journal: Journal) -> None:
        # Its figures are discarded unread, so raising about them would be an
        # error concerning data we never intended to keep.
        intent(journal, spreads=5)
        fill(journal, spreads=5, net_price_per_spread=CREDIT, source=FillSource.STREAM)
        assert (
            fill(journal, spreads=99, net_price_per_spread=CREDIT, source=FillSource.STREAM)
            is False
        )
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert stored.spreads == pytest.approx(5.0)

    def test_a_broker_fill_has_no_order_to_be_checked_against(self, journal: Journal) -> None:
        # Nothing to compare it to, and refusing it would discard the only
        # record that a liquidation happened.
        assert fill(
            journal, fill_id="liq-1", spreads=99, net_price_per_spread=5.0, client_order_id=None
        )

    def test_a_payload_without_a_limit_price_skips_the_sign_check(self, journal: Journal) -> None:
        # The payload is opaque to the journal by design. A shape we cannot
        # read costs one optional cross-check; the quantity check still holds.
        intent(journal, spreads=5, payload={"order_class": "mleg"})
        assert fill(journal, spreads=5, net_price_per_spread=1.20)
        with pytest.raises(UnitConfusionError, match="contract count"):
            fill(journal, fill_id="exec-2", spreads=5, net_price_per_spread=1.20)

    def test_a_leg_fill_is_checked_against_the_recorded_legs(self, journal: Journal) -> None:
        # Closes the units loop from the other end: record_spread_fill catches
        # contracts written into the parent, this catches a leg count that its
        # parent cannot produce. Both halves of `contracts = ratio_qty *
        # spreads` are on record, so the arithmetic is verified not trusted.
        intent(journal, spreads=2)
        fill(journal, fill_id="parent-1", spreads=2, net_price_per_spread=CREDIT)
        with pytest.raises(UnitConfusionError, match="interchanged"):
            journal.record_leg_fill(
                fill_id="leg-1",
                occ_symbol=SHORT_LEG,
                contracts=1,
                premium_per_contract=2.10,
                side="sell",
                occurred_at=at(1),
                source=FillSource.REST,
                parent_fill_id="parent-1",
                client_order_id="uw-1",
            )

    def test_at_a_ratio_of_one_the_two_units_are_indistinguishable(self, journal: Journal) -> None:
        # A limitation worth stating rather than papering over. On a 1:1
        # vertical, contracts and spreads are the same number, so writing one
        # where the other belongs is arithmetically correct and this check
        # cannot see it. It bites only on a genuine ratio spread -- which is
        # also the only case where the confusion changes anything.
        intent(journal, spreads=2)
        fill(journal, fill_id="parent-1", spreads=2, net_price_per_spread=CREDIT)
        assert journal.record_leg_fill(
            fill_id="leg-1",
            occ_symbol=SHORT_LEG,
            contracts=2,
            premium_per_contract=2.10,
            side="sell",
            occurred_at=at(1),
            source=FillSource.REST,
            parent_fill_id="parent-1",
        )

    def test_a_consistent_leg_fill_is_accepted(self, journal: Journal) -> None:
        intent(journal, spreads=2)
        fill(journal, fill_id="parent-1", spreads=2, net_price_per_spread=CREDIT)
        assert journal.record_leg_fill(
            fill_id="leg-1",
            occ_symbol=SHORT_LEG,
            contracts=2,
            premium_per_contract=2.10,
            side="sell",
            occurred_at=at(1),
            source=FillSource.REST,
            parent_fill_id="parent-1",
            client_order_id="uw-1",
        )

    def test_a_ratio_leg_multiplies_out(self, journal: Journal) -> None:
        # 3 spreads at a ratio of 2 is 6 contracts, and nothing else.
        journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=3,
            payload=PAYLOAD,
            legs=(IntentLeg(SHORT_LEG, "sell", 1), IntentLeg(LONG_LEG, "buy", 2)),
            at=at(),
        )
        fill(journal, fill_id="parent-1", spreads=3, net_price_per_spread=CREDIT)
        assert journal.record_leg_fill(
            fill_id="leg-long",
            occ_symbol=LONG_LEG,
            contracts=6,
            premium_per_contract=0.90,
            side="buy",
            occurred_at=at(1),
            source=FillSource.REST,
            parent_fill_id="parent-1",
        )
        with pytest.raises(UnitConfusionError, match="which is 6"):
            journal.record_leg_fill(
                fill_id="leg-short",
                occ_symbol=LONG_LEG,
                contracts=3,
                premium_per_contract=0.90,
                side="buy",
                occurred_at=at(2),
                source=FillSource.REST,
                parent_fill_id="parent-1",
            )

    def test_a_leg_attached_to_the_wrong_parent_is_refused(self, journal: Journal) -> None:
        # It would put a contract into a spread that never contained it.
        intent(journal, spreads=2)
        fill(journal, fill_id="parent-1", spreads=2, net_price_per_spread=CREDIT)
        with pytest.raises(JournalError, match="wrong parent"):
            journal.record_leg_fill(
                fill_id="leg-1",
                occ_symbol="SPY260911P00600000",
                contracts=2,
                premium_per_contract=2.10,
                side="sell",
                occurred_at=at(1),
                source=FillSource.REST,
                parent_fill_id="parent-1",
            )

    def test_a_leg_with_no_parent_has_nothing_to_check_against(self, journal: Journal) -> None:
        # A leg reported on its own passes: there is no spread count to
        # multiply, and inventing one would be worse than not checking.
        assert journal.record_leg_fill(
            fill_id="leg-1",
            occ_symbol=SHORT_LEG,
            contracts=17,
            premium_per_contract=2.10,
            side="sell",
            occurred_at=at(1),
            source=FillSource.REST,
        )

    def test_a_repeated_leg_fill_does_not_double_count(self, journal: Journal) -> None:
        for _ in range(2):
            journal.record_leg_fill(
                fill_id="leg-1",
                occ_symbol="XLE260911P00082000",
                contracts=5,
                premium_per_contract=2.10,
                side="sell",
                occurred_at=at(1),
                source=FillSource.REST,
                parent_fill_id="exec-1",
            )
        legs = journal.leg_fills_for("exec-1")
        assert sum(leg.contracts for leg in legs) == pytest.approx(5.0)


class TestFillConfirmation:
    def test_a_streamed_fill_is_recorded_but_not_believed(self, journal: Journal) -> None:
        # trade_updates reconnects with no cursor, no sequence number and no
        # resume token, so every disconnect is a definite gap. Hearing
        # something on that socket is not the same as knowing it.
        intent(journal)
        assert fill(journal, source=FillSource.STREAM)
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert not stored.is_confirmed
        assert journal.unconfirmed_fills() == (stored,)

    def test_a_rest_read_confirms_it_without_duplicating(self, journal: Journal) -> None:
        intent(journal)
        fill(journal, source=FillSource.STREAM)
        assert fill(journal, source=FillSource.REST) is False
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert stored.is_confirmed
        assert stored.source is FillSource.REST
        assert journal.unconfirmed_fills() == ()
        assert len(journal.spread_fills_for("uw-1")) == 1

    def test_rest_wins_when_it_disagrees_with_the_stream(self, journal: Journal) -> None:
        # The socket is a latency optimisation, never the system of record. The
        # disagreement is written down rather than smoothed over.
        intent(journal)
        fill(journal, source=FillSource.STREAM, spreads=5, net_price_per_spread=-1.20)
        fill(journal, source=FillSource.REST, spreads=3, net_price_per_spread=-1.15)
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert stored.spreads == pytest.approx(3.0)
        assert stored.net_price_per_spread == pytest.approx(-1.15)
        assert "the stream had reported" in stored.detail

    def test_a_rest_fill_is_confirmed_on_arrival(self, journal: Journal) -> None:
        intent(journal)
        fill(journal, source=FillSource.REST)
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert stored.is_confirmed

    def test_a_repeated_fill_does_not_double_count(self, journal: Journal) -> None:
        # A double-counted fill inflates realised P&L, and realised P&L is what
        # the daily loss stop measures.
        intent(journal)
        assert fill(journal, fill_id="exec-1") is True
        assert fill(journal, fill_id="exec-1") is False
        fills = journal.spread_fills_for("uw-1")
        assert len(fills) == 1
        assert sum(f.spreads for f in fills) == pytest.approx(SPREADS)

    def test_distinct_executions_both_land(self, journal: Journal) -> None:
        intent(journal)
        fill(journal, fill_id="exec-1", spreads=2, minutes=1)
        fill(journal, fill_id="exec-2", spreads=3, minutes=2)
        assert sum(f.spreads for f in journal.spread_fills_for("uw-1")) == pytest.approx(5.0)

    def test_an_empty_fill_id_is_refused(self, journal: Journal) -> None:
        intent(journal)
        with pytest.raises(ValueError, match="non-empty"):
            fill(journal, fill_id="")

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf")])
    def test_a_fill_of_no_spreads_is_refused(self, journal: Journal, bad: float) -> None:
        intent(journal)
        with pytest.raises(ValueError):
            fill(journal, spreads=bad)

    def test_fills_are_scoped_to_their_trading_day(self, journal: Journal) -> None:
        intent(journal, client_order_id="uw-0", minutes=-1440)
        intent(journal, client_order_id="uw-1")
        fill(journal, fill_id="exec-1", minutes=1)
        fill(
            journal,
            fill_id="exec-0",
            client_order_id="uw-0",
            minutes=-1440,
        )
        assert [f.fill_id for f in journal.spread_fills_on(DAY)] == ["exec-1"]


class TestFillAttribution:
    def test_our_own_fill_is_attributed_to_its_order(self, journal: Journal) -> None:
        intent(journal)
        fill(journal)
        stored = journal.spread_fill("exec-1")
        assert stored is not None
        assert stored.attribution is FillAttribution.JOURNALLED
        assert journal.unattributed_fills() == ()

    def test_a_broker_liquidation_is_kept_not_refused(self, journal: Journal) -> None:
        # If buying power is short of an ITM exercise, Alpaca sells the
        # position out within the hour before expiry (docs/GOTCHAS.md #10).
        # That arrives as a fill on an order id we never created, and throwing
        # it away would lose a real change in open risk.
        fill(journal, fill_id="liq-1", client_order_id=None, broker_order_id="alpaca-1")
        stored = journal.spread_fill("liq-1")
        assert stored is not None
        assert stored.attribution is FillAttribution.BROKER_INITIATED
        assert [f.fill_id for f in journal.unattributed_fills()] == ["liq-1"]

    def test_a_fill_naming_an_unjournalled_order_is_kept_and_flagged(
        self, journal: Journal
    ) -> None:
        # Either the write-ahead rule was broken or the order is somebody
        # else's. Both need a human, and neither is fixed by discarding the
        # only record that it happened.
        fill(journal, fill_id="ghost-1", client_order_id="never-journalled")
        stored = journal.spread_fill("ghost-1")
        assert stored is not None
        assert stored.attribution is FillAttribution.UNKNOWN_ORDER
        assert [f.fill_id for f in journal.unattributed_fills()] == ["ghost-1"]

    def test_unattributed_fills_can_be_scoped_to_a_day(self, journal: Journal) -> None:
        fill(journal, fill_id="liq-1", client_order_id=None, minutes=1)
        fill(
            journal,
            fill_id="liq-0",
            client_order_id=None,
            minutes=-1440,
        )
        assert [f.fill_id for f in journal.unattributed_fills(trading_day=DAY)] == ["liq-1"]


class TestUnresolvedFills:
    """Nothing ages out of visibility because the clock passed midnight."""

    def test_yesterdays_stream_fill_still_blocks_a_restart_today(self, journal: Journal) -> None:
        # The overnight restart is the whole point. Scoping this to today
        # would report a clean recovery while still holding an execution
        # nobody ever verified.
        yesterday = DAY - timedelta(days=1)
        intent(journal, client_order_id="uw-0", minutes=-1440)
        fill(
            journal,
            fill_id="exec-0",
            client_order_id="uw-0",
            source=FillSource.STREAM,
            minutes=-1440,
        )
        settled(journal)
        journal.record_position_snapshot([], at=at(5))
        state = journal.recover(now=at(6))
        assert RecoveryGap.UNCONFIRMED_FILLS in state.gaps
        assert [f.fill_id for f in state.unconfirmed_fills] == ["exec-0"]
        # And it is filed under the session it happened in, not today's.
        assert state.unconfirmed_fills[0].trading_day == yesterday
        assert state.trading_day == DAY

    def test_yesterdays_liquidation_still_blocks_a_restart_today(self, journal: Journal) -> None:
        fill(
            journal,
            fill_id="liq-0",
            client_order_id=None,
            minutes=-1440,
        )
        settled(journal)
        journal.record_position_snapshot([], at=at(5))
        state = journal.recover(now=at(6))
        assert RecoveryGap.UNATTRIBUTED_FILLS in state.gaps

    def test_acknowledging_a_fill_clears_the_gap(self, journal: Journal) -> None:
        # A broker liquidation is never going to become attributable, so
        # without this the gap raises forever -- and a gap that is always on
        # is a gap nobody reads.
        settled(journal)
        fill(journal, fill_id="liq-1", client_order_id=None, minutes=1)
        journal.record_position_snapshot([], at=at(5))
        assert RecoveryGap.UNATTRIBUTED_FILLS in journal.recover(now=at(6)).gaps
        journal.acknowledge_fill(
            "liq-1", detail="Alpaca sold the position out before expiry; verified flat.", at=at(7)
        )
        assert journal.recover(now=at(8)).is_clean

    def test_acknowledging_does_not_pretend_the_fill_was_confirmed(self, journal: Journal) -> None:
        # What changed is that we know about it, not that we verified it. The
        # audit trail has to keep saying so.
        intent(journal)
        fill(journal, source=FillSource.STREAM)
        acknowledged = journal.acknowledge_fill(
            "exec-1", detail="Socket dropped; position verified by hand.", at=at(5)
        )
        assert acknowledged.is_acknowledged
        assert not acknowledged.is_confirmed
        assert acknowledged.acknowledgement.startswith("Socket dropped")
        assert journal.unconfirmed_fills() == ()

    def test_acknowledging_requires_a_reason(self, journal: Journal) -> None:
        intent(journal)
        fill(journal)
        with pytest.raises(ValueError, match="requires a reason"):
            journal.acknowledge_fill("exec-1", detail="   ", at=at(5))

    def test_acknowledging_an_unknown_fill_is_loud(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="no journalled fill"):
            journal.acknowledge_fill("nope", detail="whatever", at=at(5))

    def test_a_dashboard_can_still_scope_to_one_session(self, journal: Journal) -> None:
        intent(journal, client_order_id="uw-0", minutes=-1440)
        fill(
            journal,
            fill_id="exec-0",
            client_order_id="uw-0",
            source=FillSource.STREAM,
            minutes=-1440,
        )
        intent(journal, client_order_id="uw-1")
        fill(journal, fill_id="exec-1", source=FillSource.STREAM, minutes=1)
        assert len(journal.unconfirmed_fills()) == 2
        assert [f.fill_id for f in journal.unconfirmed_fills(trading_day=DAY)] == ["exec-1"]


class TestPositionSnapshots:
    def test_a_snapshot_round_trips(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [
                PositionRecord(
                    symbol="XLE",
                    spreads=2.0,
                    max_loss=160.0,
                    unrealised_pnl=-12.5,
                    net_delta=34.0,
                    client_order_id="uw-1",
                    detail="Sep 11 82/80 put credit spread",
                )
            ],
            at=at(30),
        )
        book = journal.latest_positions()
        assert book.observed
        assert book.taken_at == at(30)
        (position,) = book.positions
        assert position.max_loss == pytest.approx(160.0)
        assert position.net_delta == pytest.approx(34.0)
        assert position.client_order_id == "uw-1"

    def test_an_observed_empty_book_is_not_an_unobserved_book(self, journal: Journal) -> None:
        # Flat and unknown are different facts, and collapsing them would let a
        # restart believe it holds nothing when it has simply never looked.
        assert journal.latest_positions().observed is False
        journal.record_position_snapshot([], at=at(30))
        book = journal.latest_positions()
        assert book.observed is True
        assert book.positions == ()

    def test_the_newest_snapshot_wins(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)], at=at(30)
        )
        journal.record_position_snapshot(
            [PositionRecord(symbol="XLF", spreads=1.0, max_loss=80.0)], at=at(60)
        )
        assert [p.symbol for p in journal.latest_positions().positions] == ["XLF"]

    def test_recent_books_come_back_newest_first(self, journal: Journal) -> None:
        journal.record_position_snapshot([], at=at(30))
        journal.record_position_snapshot([], at=at(60))
        journal.record_position_snapshot([], at=at(90))
        books = journal.recent_position_books(2)
        assert [b.taken_at for b in books] == [at(90), at(60)]


class TestVanishedPositions:
    """The paper trap.

    Assignment, exercise and expiry produce no websocket event by design, and
    on a paper account they do not reach the activities feed until the next
    day. The only same-day signal is that the position stops being listed, so
    diffing consecutive snapshots is the primary truth source rather than a
    cross-check.
    """

    def _two_snapshots(self, journal: Journal, *, second: list[PositionRecord]) -> None:
        journal.record_position_snapshot(
            [
                PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0),
                PositionRecord(symbol="XLV", spreads=1.0, max_loss=80.0),
            ],
            at=at(30),
        )
        journal.record_position_snapshot(second, at=at(60))

    def test_a_position_that_disappears_is_found(self, journal: Journal) -> None:
        self._two_snapshots(
            journal, second=[PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)]
        )
        (vanished,) = journal.vanished_positions()
        assert vanished.position.symbol == "XLV"
        assert vanished.last_seen_at == at(30)
        assert vanished.missing_at == at(60)
        # Nothing of ours closed it, so we do not know why it left.
        assert not vanished.explained

    def test_a_position_we_closed_ourselves_is_explained(self, journal: Journal) -> None:
        intent(
            journal,
            client_order_id="uw-close",
            symbol="XLV",
            spreads=1,
            payload=CLOSING_PAYLOAD,
            minutes=40,
        )
        fill(
            journal,
            fill_id="exec-close",
            symbol="XLV",
            spreads=1,
            net_price_per_spread=DEBIT,
            client_order_id="uw-close",
            minutes=45,
        )
        self._two_snapshots(
            journal, second=[PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)]
        )
        (vanished,) = journal.vanished_positions()
        assert vanished.explained
        assert [f.fill_id for f in vanished.closing_fills] == ["exec-close"]

    def test_a_broker_liquidation_does_not_count_as_an_explanation(self, journal: Journal) -> None:
        # A sell-out on an order we never created is precisely the event this
        # check exists to catch, so letting it explain the disappearance would
        # defeat the purpose.
        fill(
            journal,
            fill_id="liq-1",
            symbol="XLV",
            spreads=1,
            client_order_id=None,
            minutes=45,
        )
        self._two_snapshots(
            journal, second=[PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)]
        )
        (vanished,) = journal.vanished_positions()
        assert not vanished.explained

    def test_a_fill_outside_the_window_does_not_explain_anything(self, journal: Journal) -> None:
        # A close that happened before the position was last seen cannot be why
        # it is gone now.
        intent(
            journal,
            client_order_id="uw-close",
            symbol="XLV",
            spreads=1,
            payload=CLOSING_PAYLOAD,
            minutes=5,
        )
        fill(
            journal,
            fill_id="exec-old",
            symbol="XLV",
            spreads=1,
            net_price_per_spread=DEBIT,
            client_order_id="uw-close",
            minutes=10,
        )
        self._two_snapshots(
            journal, second=[PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)]
        )
        (vanished,) = journal.vanished_positions()
        assert not vanished.explained

    def test_nothing_vanishes_from_a_single_observation(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0)], at=at(30)
        )
        assert journal.vanished_positions() == ()

    def test_an_unchanged_book_reports_nothing(self, journal: Journal) -> None:
        self._two_snapshots(
            journal,
            second=[
                PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0),
                PositionRecord(symbol="XLV", spreads=1.0, max_loss=80.0),
            ],
        )
        assert journal.vanished_positions() == ()

    def test_a_new_position_is_not_a_disappearance(self, journal: Journal) -> None:
        self._two_snapshots(
            journal,
            second=[
                PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0),
                PositionRecord(symbol="XLV", spreads=1.0, max_loss=80.0),
                PositionRecord(symbol="XLF", spreads=1.0, max_loss=90.0),
            ],
        )
        assert journal.vanished_positions() == ()


class TestDiffCursor:
    """The diff is the only same-day record of an assignment, so it is durable.

    Comparing the two newest snapshots makes a disappearance visible for
    exactly one polling cycle. The next cycle overwrites the pair and the
    assignment is gone from the journal permanently, with nothing anywhere
    recording that it ever happened.
    """

    def test_a_vanish_survives_the_next_polling_cycle(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        journal.record_position_snapshot([], at=at(31))  # SPY assigned
        journal.record_position_snapshot([], at=at(32))  # next cycle runs first
        (vanished,) = journal.vanished_positions()
        assert vanished.position.symbol == "SPY"
        assert vanished.last_seen_at == at(30)

    def test_every_undiffed_pair_is_reported_not_just_the_newest(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [
                PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0),
                PositionRecord(symbol="XLE", spreads=1.0, max_loss=90.0),
            ],
            at=at(30),
        )
        journal.record_position_snapshot(
            [PositionRecord(symbol="XLE", spreads=1.0, max_loss=90.0)], at=at(31)
        )
        journal.record_position_snapshot([], at=at(32))
        assert sorted(v.position.symbol for v in journal.vanished_positions()) == ["SPY", "XLE"]

    def test_advancing_the_cursor_consumes_them(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        last = journal.record_position_snapshot([], at=at(31))
        assert journal.vanished_positions()
        journal.mark_positions_diffed(last, at=at(32))
        assert journal.vanished_positions() == ()
        assert journal.position_diff_cursor() == last
        assert journal.undiffed_snapshots() == 0

    def test_a_crash_before_advancing_re_reports_rather_than_losing_it(self, db_path: Path) -> None:
        # Detection is at-least-once on purpose. The cursor is advanced by the
        # caller, after the event is recorded, so a crash in between costs a
        # repeat rather than a silent loss.
        with Journal(db_path) as j:
            snapshot = j.record_position_snapshot(
                [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
            )
            j.record_position_snapshot([], at=at(31))
            (seen,) = j.vanished_positions()
            j.record_position_event(
                symbol="SPY",
                cause=PositionEventCause.UNKNOWN,
                from_snapshot_id=seen.from_snapshot_id,
                at=at(31),
            )
            # ... and the process dies here, before mark_positions_diffed.
            assert seen.from_snapshot_id == snapshot

        with Journal(db_path) as j:
            (again,) = j.vanished_positions()
            # Re-recording is idempotent, so at-least-once detection produces
            # exactly one event.
            j.record_position_event(
                symbol="SPY",
                cause=PositionEventCause.UNKNOWN,
                from_snapshot_id=again.from_snapshot_id,
                at=at(40),
            )
            assert len(j.position_events()) == 1

    def test_the_cursor_survives_a_restart(self, db_path: Path) -> None:
        with Journal(db_path) as j:
            j.record_position_snapshot(
                [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
            )
            last = j.record_position_snapshot([], at=at(31))
            j.mark_positions_diffed(last, at=at(32))

        with Journal(db_path) as j:
            assert j.position_diff_cursor() == last
            assert j.vanished_positions() == ()

    def test_the_cursor_never_moves_backwards(self, journal: Journal) -> None:
        # Re-running an older diff must not make a handled disappearance
        # pending again.
        first = journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        last = journal.record_position_snapshot([], at=at(31))
        journal.mark_positions_diffed(last, at=at(32))
        assert journal.mark_positions_diffed(first, at=at(33)) == last
        assert journal.vanished_positions() == ()

    def test_a_cursor_onto_an_unknown_snapshot_is_refused(self, journal: Journal) -> None:
        with pytest.raises(JournalError, match="no position snapshot"):
            journal.mark_positions_diffed(999, at=at(30))

    def test_one_snapshot_is_a_baseline_not_a_backlog(self, journal: Journal) -> None:
        journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        assert journal.undiffed_snapshots() == 0
        assert journal.vanished_positions() == ()

    def test_an_undiffed_backlog_is_counted(self, journal: Journal) -> None:
        for minute in (30, 31, 32):
            journal.record_position_snapshot([], at=at(minute))
        assert journal.undiffed_snapshots() == 2

    def test_a_position_that_returns_still_records_its_departure(self, journal: Journal) -> None:
        held = [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)]
        journal.record_position_snapshot(held, at=at(30))
        journal.record_position_snapshot([], at=at(31))
        journal.record_position_snapshot(held, at=at(32))
        (vanished,) = journal.vanished_positions()
        assert vanished.position.symbol == "SPY"
        assert vanished.missing_at == at(31)


class TestPositionEvents:
    def test_an_unexplained_exit_is_recorded_as_unknown(self, journal: Journal) -> None:
        # Recording it as a clean close would quietly turn an assignment, an
        # expiry or a liquidation into a trade we meant to make.
        event = journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            spreads=1.0,
            from_snapshot_id=1,
            at=at(60),
        )
        assert event.cause is PositionEventCause.UNKNOWN
        assert event.evidence is PositionEventEvidence.INFERRED_FROM_SNAPSHOT
        assert not event.is_confirmed
        assert journal.unexplained_position_events() == (event,)

    def test_an_exit_we_made_needs_no_explanation(self, journal: Journal) -> None:
        journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.CLOSED_BY_US,
            from_snapshot_id=1,
            at=at(60),
        )
        assert journal.unexplained_position_events() == ()

    def test_re_detecting_the_same_exit_does_not_duplicate_it(self, journal: Journal) -> None:
        # A monitor that runs twice in a cycle must not turn one assignment
        # into two.
        first = journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(60),
        )
        second = journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(61),
        )
        assert first.id == second.id
        assert len(journal.position_events()) == 1

    def test_tomorrows_activity_attaches_to_todays_inference(self, journal: Journal) -> None:
        # Paper accounts sync non-trade activities at the start of the
        # following day, so the authoritative record always arrives after we
        # have already seen the position go. Two rows would read as two
        # assignments.
        inferred = journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            spreads=1.0,
            from_snapshot_id=1,
            at=at(60),
        )
        confirmed = journal.confirm_position_event(
            activity_id="OPASN-9f2",
            symbol="XLV",
            trading_day=DAY,
            cause=PositionEventCause.ASSIGNMENT,
            detail="Short put assigned at expiry.",
            at=at(1440),
        )
        assert confirmed.id == inferred.id
        assert confirmed.cause is PositionEventCause.ASSIGNMENT
        assert confirmed.evidence is PositionEventEvidence.CONFIRMED_BY_ACTIVITY
        assert confirmed.is_confirmed
        assert len(journal.position_events()) == 1
        assert journal.unexplained_position_events() == ()

    def test_a_replayed_activity_confirms_only_once(self, journal: Journal) -> None:
        journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(60),
        )
        first = journal.confirm_position_event(
            activity_id="OPASN-9f2",
            symbol="XLV",
            trading_day=DAY,
            cause=PositionEventCause.ASSIGNMENT,
            at=at(1440),
        )
        second = journal.confirm_position_event(
            activity_id="OPASN-9f2",
            symbol="XLV",
            trading_day=DAY,
            cause=PositionEventCause.ASSIGNMENT,
            at=at(1500),
        )
        assert first.id == second.id
        assert len(journal.position_events()) == 1

    def test_an_activity_we_never_inferred_is_still_recorded(self, journal: Journal) -> None:
        # The position came and went between two snapshots, or the agent was
        # down. The activity is the only record, so it stands on its own.
        event = journal.confirm_position_event(
            activity_id="OPEXP-1",
            symbol="XLU",
            trading_day=DAY,
            cause=PositionEventCause.EXPIRY,
            spreads=2.0,
            at=at(1440),
        )
        assert event.evidence is PositionEventEvidence.CONFIRMED_BY_ACTIVITY
        assert event.spreads == pytest.approx(2.0)
        assert journal.unexplained_position_events() == ()

    def test_confirmation_does_not_steal_another_days_inference(self, journal: Journal) -> None:
        # The stale inference was filed under yesterday's session, derived from
        # its own detection time.
        stale = journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(-1440),
        )
        fresh = journal.confirm_position_event(
            activity_id="OPASN-1",
            symbol="XLV",
            trading_day=DAY,
            cause=PositionEventCause.ASSIGNMENT,
            at=at(1440),
        )
        assert fresh.id != stale.id
        assert [e.id for e in journal.unexplained_position_events()] == [stale.id]

    def test_an_empty_activity_id_is_refused(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            journal.confirm_position_event(
                activity_id="  ",
                symbol="XLV",
                trading_day=DAY,
                cause=PositionEventCause.ASSIGNMENT,
                at=at(1440),
            )

    def test_events_can_be_scoped_to_a_day(self, journal: Journal) -> None:
        journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(60),
        )
        journal.record_position_event(
            symbol="XLU",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=0,
            at=at(-1440),
        )
        assert [e.symbol for e in journal.position_events(trading_day=DAY)] == ["XLV"]


class TestReconciliationClock:
    def test_a_successful_pass_sets_the_clock(self, journal: Journal) -> None:
        journal.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at(10))
        last = journal.last_reconciliation()
        assert last is not None
        assert last.at == at(10)
        assert journal.view_age(now=at(13)) == timedelta(minutes=3)

    def test_a_failed_pass_does_not_refresh_it(self, journal: Journal) -> None:
        # An attempt that errored out tells us nothing about the broker and
        # everything about the network, so it must not make our view look
        # fresher than it is.
        journal.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at(10))
        journal.record_reconciliation(
            scope=ReconciliationScope.FULL, ok=False, detail="timeout", at=at(20)
        )
        last = journal.last_reconciliation()
        assert last is not None
        assert last.at == at(10)

    def test_never_reconciled_is_staler_than_any_number(self, journal: Journal) -> None:
        assert journal.last_reconciliation() is None
        assert journal.view_age(now=at(10)) is None

    def test_scopes_keep_their_own_clocks(self, journal: Journal) -> None:
        journal.record_reconciliation(scope=ReconciliationScope.ORDERS, ok=True, at=at(10))
        assert journal.last_reconciliation(ReconciliationScope.ORDERS) is not None
        assert journal.last_reconciliation(ReconciliationScope.POSITIONS) is None


class TestTradingDay:
    """The session a write belongs to is derived, never supplied.

    UTC would very nearly do: UTC midnight is 19:00-20:00 ET, well after the
    options close, so inside a regular session the UTC and exchange dates
    agree. The hazard runs the other way, and it is not symmetric -- see
    `test_a_late_evening_write_cannot_claim_tomorrow`.
    """

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            # 09:30 ET, the open: UTC and exchange dates agree.
            ("2026-09-02T13:30:00+00:00", date(2026, 9, 2)),
            # 22:05 ET Wednesday is already Thursday in UTC.
            ("2026-09-03T02:05:00+00:00", date(2026, 9, 2)),
            # 19:30 ET, after UTC midnight has NOT yet arrived in summer.
            ("2026-09-02T23:30:00+00:00", date(2026, 9, 2)),
            # Winter, when the offset is five hours rather than four.
            ("2026-01-15T14:30:00+00:00", date(2026, 1, 15)),
        ],
    )
    def test_the_exchange_clock_decides(self, moment: str, expected: date) -> None:
        assert trading_day_of(datetime.fromisoformat(moment)) == expected

    def test_a_naive_moment_has_no_trading_day(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            trading_day_of(datetime(2026, 9, 2, 9, 30))  # noqa: DTZ001

    def test_a_late_evening_write_cannot_claim_tomorrow(self, journal: Journal) -> None:
        # The reviewed failure. A UTC-derived day would file 22:05 ET Thursday
        # as Friday's baseline; first-write-wins would then discard Friday
        # morning's real 100,000 in favour of Thursday night's 91,000, and the
        # daily stop would measure against a baseline 9% low.
        thursday_night = datetime.fromisoformat("2026-09-04T02:05:00+00:00")
        friday_open = datetime.fromisoformat("2026-09-04T13:30:00+00:00")
        journal.record_session_open_equity(equity=91_000.0, at=thursday_night)
        kept = journal.record_session_open_equity(equity=100_000.0, at=friday_open)
        assert kept == pytest.approx(100_000.0)
        assert journal.session_open_equity(date(2026, 9, 4)) == pytest.approx(100_000.0)
        # It belongs to Thursday, which is where it went.
        assert journal.session_open_equity(date(2026, 9, 3)) == pytest.approx(91_000.0)

    def test_a_fill_belongs_to_the_session_it_happened_in(self, journal: Journal) -> None:
        intent(journal, minutes=-1440)
        fill(journal, minutes=-1440)
        (stored,) = journal.spread_fills_on(DAY - timedelta(days=1))
        assert stored.trading_day == DAY - timedelta(days=1)
        assert journal.spread_fills_on(DAY) == ()

    def test_recovery_derives_its_own_day(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        assert journal.recover(now=at(30)).trading_day == DAY
        assert journal.recover(now=at(1440)).trading_day == DAY + timedelta(days=1)


class TestKillSwitch:
    """A safety decision has to outlive the process that made it.

    An agent that trips its own switch after a bad event and then crashes must
    not come back up with the switch off and resume trading.
    """

    def test_a_fresh_journal_is_not_stopped(self, journal: Journal) -> None:
        state = journal.kill_switch()
        assert not state.engaged
        assert state.may_trade
        assert journal.kill_switch_history() == ()

    def test_engaging_survives_a_crash(self, db_path: Path) -> None:
        crashed = Journal(db_path)
        crashed.engage_kill_switch(
            reason="Three consecutive stop-outs inside an hour.",
            actor=KillSwitchActor.AGENT,
            at=at(60),
        )
        crashed.close()

        with Journal(db_path) as restarted:
            state = restarted.kill_switch()
            assert state.engaged
            assert not state.may_trade
            assert state.actor is KillSwitchActor.AGENT
            assert state.changed_at == at(60)
            assert "stop-outs" in state.reason

    def test_re_engaging_is_not_an_error_and_keeps_the_first_reason(self, journal: Journal) -> None:
        # The first reason explains why we stopped. A later re-engagement is
        # the same decision being taken again by code that could not know it
        # had already been taken.
        journal.engage_kill_switch(
            reason="Three consecutive stop-outs.", actor=KillSwitchActor.AGENT, at=at(60)
        )
        again = journal.engage_kill_switch(
            reason="Regime turned hostile.", actor=KillSwitchActor.RISK, at=at(65)
        )
        assert again.engaged
        assert again.reason == "Three consecutive stop-outs."
        assert again.actor is KillSwitchActor.AGENT
        assert again.changed_at == at(60)

    def test_every_call_is_still_on_the_record(self, journal: Journal) -> None:
        # Idempotent must not mean invisible: the repetition is evidence.
        journal.engage_kill_switch(reason="first", actor=KillSwitchActor.AGENT, at=at(60))
        journal.engage_kill_switch(reason="second", actor=KillSwitchActor.RISK, at=at(65))
        history = journal.kill_switch_history()
        assert [(e.reason, e.actor) for e in history] == [
            ("second", KillSwitchActor.RISK),
            ("first", KillSwitchActor.AGENT),
        ]

    def test_it_takes_an_explicit_decision_to_resume(self, journal: Journal) -> None:
        journal.engage_kill_switch(reason="bad day", actor=KillSwitchActor.AGENT, at=at(60))
        released = journal.disengage_kill_switch(
            reason="Reviewed; cause was a data outage, not the strategy.",
            actor=KillSwitchActor.OPERATOR,
            at=at(600),
        )
        assert released.may_trade
        assert released.actor is KillSwitchActor.OPERATOR
        assert journal.kill_switch().changed_at == at(600)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_both_directions_require_a_reason(self, journal: Journal, blank: str) -> None:
        with pytest.raises(ValueError, match="requires a reason"):
            journal.engage_kill_switch(reason=blank, actor=KillSwitchActor.AGENT, at=at(60))
        with pytest.raises(ValueError, match="requires a reason"):
            journal.disengage_kill_switch(reason=blank, actor=KillSwitchActor.OPERATOR, at=at(60))

    def test_recovery_reports_it_first_and_refuses_to_trade(self, journal: Journal) -> None:
        # Prominence is the point. A safety stop that needs its own separate
        # lookup is a safety stop somebody forgets to look up.
        settled(journal)
        journal.record_position_snapshot([], at=at(5))
        assert journal.recover(now=at(6)).may_trade
        journal.engage_kill_switch(
            reason="Assignment on XLV with no matching fill.",
            actor=KillSwitchActor.AGENT,
            at=at(7),
        )
        state = journal.recover(now=at(8))
        assert state.gaps[0] is RecoveryGap.KILL_SWITCH_ENGAGED
        assert not state.may_trade
        assert not state.is_clean
        assert state.kill_switch.engaged
        assert state.detail[0].startswith("KILL SWITCH ENGAGED by agent")

    def test_it_does_not_age_out(self, journal: Journal) -> None:
        # No amount of elapsed time releases it.
        settled(journal, minutes=1430)
        journal.record_position_snapshot([], at=at(1430))
        journal.engage_kill_switch(reason="bad day", actor=KillSwitchActor.AGENT, at=at())
        assert not journal.recover(now=at(1431)).may_trade


class TestSessionOpenEquity:
    def test_the_first_write_of_the_day_wins(self, journal: Journal) -> None:
        # The daily loss stop measures against session-open equity. Letting it
        # be rewritten mid-session would make the baseline drift with P&L, and
        # a stop whose baseline follows the loss never fires.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        later = journal.record_session_open_equity(equity=98_400.0, at=at(120))
        assert later == pytest.approx(EQUITY)
        assert journal.session_open_equity(DAY) == pytest.approx(EQUITY)

    def test_each_day_keeps_its_own_opening(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_session_open_equity(equity=101_000.0, at=at(1440))
        assert journal.session_open_equity(DAY) == pytest.approx(EQUITY)
        assert journal.session_open_equity(DAY + timedelta(days=1)) == pytest.approx(101_000.0)

    def test_an_unrecorded_day_reads_as_unknown(self, journal: Journal) -> None:
        assert journal.session_open_equity(DAY) is None

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_unusable_equity_is_refused(self, journal: Journal, bad: float) -> None:
        # Storing it would produce a baseline that silently disables the stop.
        with pytest.raises(ValueError, match="finite"):
            journal.record_session_open_equity(equity=bad, at=at())


class TestDisputedSessionOpen:
    def test_a_discarded_figure_is_kept_not_dropped(self, journal: Journal) -> None:
        # Winning the argument is not the same as being right. Two parts of the
        # system disagreeing about where the day started is exactly the kind of
        # thing whose only symptom is a stop firing at the wrong level.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_session_open_equity(equity=98_400.0, at=at(120))
        (rejection,) = journal.rejected_session_opens(trading_day=DAY)
        assert rejection.offered == pytest.approx(98_400.0)
        assert rejection.kept == pytest.approx(EQUITY)
        assert rejection.drift_pct == pytest.approx(-1.6)
        assert rejection.offered_at == at(120)

    def test_re_recording_the_same_figure_is_not_a_dispute(self, journal: Journal) -> None:
        # A restart that re-reads and re-writes the same opening equity has not
        # disagreed with anything.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_session_open_equity(equity=EQUITY, at=at(120))
        assert journal.rejected_session_opens() == ()

    def test_a_dispute_blocks_a_clean_recovery(self, journal: Journal) -> None:
        settled(journal)
        journal.record_position_snapshot([], at=at(5))
        journal.record_session_open_equity(equity=91_000.0, at=at(6))
        state = journal.recover(now=at(7))
        assert RecoveryGap.SESSION_EQUITY_DISPUTED in state.gaps
        assert [r.offered for r in state.rejected_session_opens] == [pytest.approx(91_000.0)]
        assert any("disagree about where the day started" in d for d in state.detail)

    def test_a_dispute_does_not_follow_us_into_the_next_session(self, journal: Journal) -> None:
        # Unlike an unconfirmed fill, the baseline is a per-day fact. Yesterday's
        # disagreement is settled by yesterday ending.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_session_open_equity(equity=91_000.0, at=at(120))
        settled(journal, minutes=1430)
        journal.record_session_open_equity(equity=101_000.0, at=at(1430))
        journal.record_position_snapshot([], at=at(1430))
        state = journal.recover(now=at(1431))
        assert RecoveryGap.SESSION_EQUITY_DISPUTED not in state.gaps

    def test_a_missed_open_can_be_reconstructed_from_an_equity_reading(
        self, journal: Journal
    ) -> None:
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0.0, equity=99_500.0, at=at(5))
        journal.record_pnl(
            source=PnlSource.OFFICIAL, realised_pnl=-120.0, equity=99_380.0, at=at(90)
        )
        candidate = journal.session_open_candidate(DAY)
        assert candidate is not None
        # The EARLIEST reading, since that is the closest thing to an open.
        assert candidate.equity == pytest.approx(99_500.0)
        assert candidate.at == at(5)

    def test_the_reconstruction_is_offered_never_adopted(self, journal: Journal) -> None:
        # Writing it in automatically would manufacture a baseline out of a
        # guess, which is the failure this whole area exists to avoid.
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0.0, equity=99_500.0, at=at(5))
        state = journal.recover(now=at(6))
        assert RecoveryGap.SESSION_EQUITY_MISSING in state.gaps
        assert state.session_open_equity is None
        assert journal.session_open_equity(DAY) is None
        assert state.session_open_candidate is not None
        assert any("adopt it deliberately" in d for d in state.detail)

    def test_a_day_with_no_equity_reading_cannot_be_reconstructed(self, journal: Journal) -> None:
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0.0, at=at(5))
        assert journal.session_open_candidate(DAY) is None
        assert any("No equity reading exists" in d for d in journal.recover(now=at(6)).detail)


class TestOrderLegs:
    """The map from a contract back to the spread holding it."""

    LEGS = (
        IntentLeg("XLE260911P00082000", "sell", 1, "sell_to_open"),
        IntentLeg("XLE260911P00080000", "buy", 1, "buy_to_open"),
    )

    def test_legs_are_queryable_rather_than_buried_in_the_payload(self, journal: Journal) -> None:
        journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=2,
            payload=PAYLOAD,
            legs=self.LEGS,
            at=at(),
        )
        assert [leg.occ_symbol for leg in journal.legs_for("uw-1")] == [
            "XLE260911P00080000",
            "XLE260911P00082000",
        ]
        assert [o.client_order_id for o in journal.orders_holding("XLE260911P00082000")] == ["uw-1"]
        assert journal.orders_holding("XLE260911P00075000") == ()

    def test_an_order_cannot_be_journalled_without_them(self, journal: Journal) -> None:
        # orders_holding() reaches the map through a join, so a legless order
        # would not be unmapped -- it would be invisible, and the query would
        # answer "nothing holds this contract". Partial looks like an answer;
        # absent fails loudly. So the map is total by construction.
        with pytest.raises(ValueError, match="must record its legs"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=(),
                at=at(),
            )
        assert journal.order_history() == ()

    def test_every_journalled_order_answers_orders_holding(self, journal: Journal) -> None:
        # The property the requirement buys: no order can exist that the map
        # does not know about.
        intent(journal, client_order_id="uw-1")
        intent(journal, client_order_id="uw-2", minutes=5)
        for order in journal.order_history():
            legs = journal.legs_for(order.client_order_id)
            assert legs
            for leg in legs:
                held = journal.orders_holding(leg.occ_symbol)
                assert order.client_order_id in [o.client_order_id for o in held]

    @pytest.mark.parametrize("count", [1, 5])
    def test_a_structure_that_is_not_a_spread_is_refused(
        self, journal: Journal, count: int
    ) -> None:
        legs = tuple(
            IntentLeg(f"XLE260911P0008{i}000", "sell" if i % 2 else "buy", 1) for i in range(count)
        )
        with pytest.raises(ValueError, match="between 2 and 4 legs"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=legs,
                at=at(),
            )

    def test_unreduced_ratios_are_refused(self, journal: Journal) -> None:
        # 2:2 is 1:1 at twice the size. Only one of those readings can be
        # right, and the size belongs in the order's spread count.
        with pytest.raises(ValueError, match="common factor of 2"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=(IntentLeg(SHORT_LEG, "sell", 2), IntentLeg(LONG_LEG, "buy", 2)),
                at=at(),
            )

    def test_a_genuinely_unequal_ratio_is_allowed(self, journal: Journal) -> None:
        # 1:2 shares no factor, so it is a real ratio spread rather than a
        # doubled vertical.
        journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=2,
            payload=PAYLOAD,
            legs=(IntentLeg(SHORT_LEG, "sell", 1), IntentLeg(LONG_LEG, "buy", 2)),
            at=at(),
        )
        assert [leg.ratio_qty for leg in journal.legs_for("uw-1")] == [2, 1]

    def test_a_duplicate_intent_with_the_same_legs_is_still_idempotent(
        self, journal: Journal
    ) -> None:
        for minute in (0, 3):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=self.LEGS,
                at=at(minute),
            )
        assert len(journal.order_history()) == 1
        assert len(journal.legs_for("uw-1")) == 2

    def test_reusing_an_id_for_different_legs_is_refused(self, journal: Journal) -> None:
        journal.record_intent(
            client_order_id="uw-1",
            cycle_id="c",
            symbol="XLE",
            spreads=2,
            payload=PAYLOAD,
            legs=self.LEGS,
            at=at(),
        )
        with pytest.raises(ConflictingIntentError):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=(
                    IntentLeg(SHORT_LEG, "sell", 1),
                    IntentLeg("XLE260911P00079000", "buy", 1),
                ),
                at=at(1),
            )

    def test_the_same_contract_twice_in_one_order_is_refused(self, journal: Journal) -> None:
        # It would make the contract-to-strategy map wrong in a way nothing
        # downstream could detect.
        with pytest.raises(ValueError, match="appears twice"):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=(
                    IntentLeg("XLE260911P00082000", "sell", 1),
                    IntentLeg("XLE260911P00082000", "buy", 1),
                ),
                at=at(),
            )

    @pytest.mark.parametrize(
        "bad", [IntentLeg("XLE260911P00082000", "short", 1), IntentLeg("X", "buy", 0)]
    )
    def test_an_unusable_leg_is_refused(self, journal: Journal, bad: IntentLeg) -> None:
        with pytest.raises(ValueError):
            journal.record_intent(
                client_order_id="uw-1",
                cycle_id="c",
                symbol="XLE",
                spreads=2,
                payload=PAYLOAD,
                legs=(bad,),
                at=at(),
            )


class TestPnlSnapshots:
    def test_official_and_shadow_are_kept_apart(self, journal: Journal) -> None:
        # The paper fill model is undocumented, so the two series are reported
        # side by side rather than reconciled. See docs/GOTCHAS.md #3.
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=140.0, at=at(60))
        journal.record_pnl(source=PnlSource.SHADOW, realised_pnl=96.0, at=at(60))
        official = journal.latest_pnl(trading_day=DAY, source=PnlSource.OFFICIAL)
        shadow = journal.latest_pnl(trading_day=DAY, source=PnlSource.SHADOW)
        assert official is not None
        assert shadow is not None
        assert official.realised_pnl == pytest.approx(140.0)
        assert shadow.realised_pnl == pytest.approx(96.0)

    def test_the_newest_reading_of_the_day_is_returned(self, journal: Journal) -> None:
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=10.0, at=at(60))
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-40.0, at=at(120))
        latest = journal.latest_pnl(trading_day=DAY)
        assert latest is not None
        assert latest.realised_pnl == pytest.approx(-40.0)

    def test_a_day_with_no_reading_is_none(self, journal: Journal) -> None:
        assert journal.latest_pnl(trading_day=DAY) is None

    def test_a_nonsense_pnl_is_refused(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="finite"):
            journal.record_pnl(
                source=PnlSource.OFFICIAL,
                realised_pnl=float("nan"),
                at=at(60),
            )


class TestRegimeVerdicts:
    def test_verdicts_are_recorded_whether_or_not_they_block(self, journal: Journal) -> None:
        # The filter should be judged on whether it fired at the right times,
        # which needs the quiet days on the record too.
        journal.record_regime_verdict(allowed=True, at=at(1))
        journal.record_regime_verdict(
            allowed=False,
            blocks=["term_structure_inverted"],
            detail=["Near/far ATM IV ratio 1.08."],
            context={"ratio": 1.08},
            at=at(2),
        )
        history = journal.regime_history()
        assert [v.allowed for v in history] == [False, True]
        assert history[0].blocks == ("term_structure_inverted",)
        assert history[0].context == {"ratio": 1.08}

    def test_a_block_without_a_reason_is_refused(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="at least one reason"):
            journal.record_regime_verdict(allowed=False, at=at(1))


class TestRecovery:
    def test_an_empty_database_knows_what_it_does_not_know(self, journal: Journal) -> None:
        state = journal.recover(now=at(10))
        assert state.unreconciled_orders == ()
        assert state.open_positions == ()
        # Nothing filled and nothing reported: the day has realised nothing,
        # and that is a fact rather than a guess.
        assert state.realised_pnl_today == pytest.approx(0.0)
        assert state.session_open_equity is None
        assert set(state.gaps) == {RecoveryGap.SESSION_EQUITY_MISSING, RecoveryGap.VIEW_STALE}
        assert not state.is_clean

    def test_a_fully_recorded_session_recovers_clean(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        intent(journal, spreads=2)
        journal.mark_submitted("uw-1", broker_order_id="b-1", at=at(1))
        fill(journal, spreads=2, minutes=2, source=FillSource.REST)
        journal.mark_status(
            "uw-1", OrderStatus.FILLED, spreads_filled=2, net_price_per_spread=CREDIT, at=at(2)
        )
        journal.record_position_snapshot(
            [PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0, net_delta=34.0)],
            at=at(3),
        )
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0.0, at=at(4))
        journal.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at(5))

        state = journal.recover(now=at(6))
        assert state.is_clean
        assert state.gaps == ()
        assert state.session_open_equity == pytest.approx(EQUITY)
        assert state.realised_pnl_today == pytest.approx(0.0)
        assert [p.symbol for p in state.open_positions] == ["XLE"]
        assert state.view_age == timedelta(minutes=1)

    def test_recovery_survives_a_restart_intact(self, db_path: Path) -> None:
        with Journal(db_path) as j:
            settled(j)
            intent(j, spreads=2)
            j.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=2, at=at(2))
            j.record_position_snapshot(
                [PositionRecord(symbol="XLE", spreads=2.0, max_loss=160.0, net_delta=34.0)],
                at=at(3),
            )
            before = j.recover(now=at(6))

        with Journal(db_path) as j:
            assert j.recover(now=at(6)) == before

    def test_an_unsubmitted_intent_blocks_a_clean_recovery(self, db_path: Path) -> None:
        with Journal(db_path) as j:
            settled(j)
            intent(j, client_order_id="uw-1", spreads=5)
            j.record_position_snapshot([], at=at(6))

        with Journal(db_path) as j:
            state = j.recover(now=at(7))
            assert RecoveryGap.UNRECONCILED_ORDERS in state.gaps
            assert [o.client_order_id for o in state.unreconciled_orders] == ["uw-1"]
            # The gap has to be readable by whoever is woken up by it, and it
            # has to say how much is at stake.
            assert any("5 spread(s) possibly working" in d for d in state.detail)

    def test_a_stale_view_blocks_a_clean_recovery(self, journal: Journal) -> None:
        # At a restart this is expected rather than alarming: it means
        # reconcile before trading.
        settled(journal)
        journal.record_position_snapshot([], at=at(6))
        state = journal.recover(now=at(120), max_view_age=timedelta(minutes=5))
        assert RecoveryGap.VIEW_STALE in state.gaps
        assert state.view_age == timedelta(minutes=115)
        assert any("beyond the" in d for d in state.detail)

    def test_never_reconciled_is_stale_whatever_the_limit(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        state = journal.recover(now=at(1), max_view_age=timedelta(days=365))
        assert RecoveryGap.VIEW_STALE in state.gaps
        assert state.last_reconciled_at is None
        assert any("never been reconciled" in d for d in state.detail)

    def test_stream_only_fills_block_a_clean_recovery(self, journal: Journal) -> None:
        settled(journal, minutes=10)
        intent(journal, spreads=2)
        fill(journal, spreads=2, minutes=2, source=FillSource.STREAM)
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=2, at=at(2))
        journal.record_position_snapshot([], at=at(3))
        state = journal.recover(now=at(11))
        assert RecoveryGap.UNCONFIRMED_FILLS in state.gaps
        assert [f.fill_id for f in state.unconfirmed_fills] == ["exec-1"]
        assert any("latency optimisation" in d for d in state.detail)

    def test_a_broker_liquidation_blocks_a_clean_recovery(self, journal: Journal) -> None:
        settled(journal, minutes=10)
        fill(journal, fill_id="liq-1", client_order_id=None, minutes=2)
        journal.record_position_snapshot([], at=at(3))
        state = journal.recover(now=at(11))
        assert RecoveryGap.UNATTRIBUTED_FILLS in state.gaps
        assert [f.fill_id for f in state.unattributed_fills] == ["liq-1"]

    def test_an_unexplained_exit_blocks_a_clean_recovery(self, journal: Journal) -> None:
        settled(journal, minutes=10)
        journal.record_position_snapshot([], at=at(3))
        journal.record_position_event(
            symbol="XLV",
            cause=PositionEventCause.UNKNOWN,
            from_snapshot_id=1,
            at=at(4),
        )
        state = journal.recover(now=at(11))
        assert RecoveryGap.UNEXPLAINED_POSITION_EXITS in state.gaps
        assert [e.symbol for e in state.unexplained_exits] == ["XLV"]
        assert any("arrives tomorrow" in d for d in state.detail)

    def test_an_undiffed_backlog_blocks_a_clean_recovery(self, journal: Journal) -> None:
        # A restart that has not diffed its snapshots does not know whether a
        # position was assigned overnight.
        settled(journal)
        journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        journal.record_position_snapshot([], at=at(31))
        state = journal.recover(now=at(32))
        assert RecoveryGap.POSITION_DIFFS_PENDING in state.gaps
        assert state.undiffed_snapshots == 1
        assert [v.position.symbol for v in state.pending_vanishes] == ["SPY"]
        assert any("only same-day evidence" in d for d in state.detail)

    def test_a_drained_diff_recovers_clean(self, journal: Journal) -> None:
        settled(journal, minutes=32)
        journal.record_position_snapshot(
            [PositionRecord(symbol="SPY", spreads=1.0, max_loss=100.0)], at=at(30)
        )
        last = journal.record_position_snapshot([], at=at(31))
        journal.record_position_event(
            symbol="SPY",
            cause=PositionEventCause.CLOSED_BY_US,
            from_snapshot_id=last - 1,
            at=at(31),
        )
        journal.mark_positions_diffed(last, at=at(32))
        assert journal.recover(now=at(33)).is_clean

    def test_a_stale_pnl_snapshot_reads_as_unknown(self, journal: Journal) -> None:
        # A fill landed after the last P&L reading, so that reading understates
        # the day. Handing it over as a number would let the daily loss stop
        # measure against a loss that has already grown.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        intent(journal, spreads=2)
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-100.0, at=at(10))
        fill(journal, spreads=2, minutes=20)
        state = journal.recover(now=at(21))
        assert state.realised_pnl_today is None
        assert RecoveryGap.REALISED_PNL_UNKNOWN in state.gaps
        assert any("stale" in d for d in state.detail)

    def test_fills_with_no_pnl_reading_at_all_read_as_unknown(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        intent(journal, spreads=2)
        fill(journal, spreads=2, minutes=20)
        state = journal.recover(now=at(21))
        assert state.realised_pnl_today is None
        assert RecoveryGap.REALISED_PNL_UNKNOWN in state.gaps

    def test_a_pnl_reading_after_the_last_fill_is_trusted(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        intent(journal, spreads=2)
        fill(journal, spreads=2, minutes=20)
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-100.0, at=at(21))
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=2, at=at(21))
        journal.record_position_snapshot([], at=at(22))
        journal.record_reconciliation(scope=ReconciliationScope.FULL, ok=True, at=at(22))
        state = journal.recover(now=at(23))
        assert state.realised_pnl_today == pytest.approx(-100.0)
        assert state.is_clean

    def test_recovery_reads_the_official_series_not_the_shadow(self, journal: Journal) -> None:
        # The daily loss stop measures the real account. The shadow figure is
        # for the write-up, not for the gate.
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-100.0, at=at(10))
        journal.record_pnl(source=PnlSource.SHADOW, realised_pnl=-260.0, at=at(11))
        assert journal.recover(now=at(12)).realised_pnl_today == pytest.approx(-100.0)

    def test_yesterdays_pnl_does_not_leak_into_today(self, journal: Journal) -> None:
        journal.record_session_open_equity(equity=EQUITY, at=at())
        journal.record_pnl(
            source=PnlSource.OFFICIAL,
            realised_pnl=-900.0,
            at=at(-1440),
        )
        assert journal.recover(now=at(1)).realised_pnl_today == pytest.approx(0.0)

    def test_orders_without_a_snapshot_leave_the_book_unknown(self, journal: Journal) -> None:
        # We have traded and never looked at the book. That is not the same as
        # being flat, and reporting it as flat would understate open risk.
        settled(journal)
        intent(journal, spreads=2)
        journal.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=2, at=at(1))
        state = journal.recover(now=at(6))
        assert RecoveryGap.POSITIONS_UNOBSERVED in state.gaps
        assert state.book.observed is False


class TestConcurrentAccess:
    def test_a_second_connection_sees_committed_writes(self, db_path: Path) -> None:
        # The dashboard reads the same file the agent writes. WAL is what keeps
        # the reader from blocking the trading cycle.
        with Journal(db_path) as writer, Journal(db_path) as reader:
            assert reader.order_history() == ()
            intent(writer, client_order_id="uw-1")
            assert [o.client_order_id for o in reader.order_history()] == ["uw-1"]

    def test_writes_interleave_across_connections(self, db_path: Path) -> None:
        with Journal(db_path) as first, Journal(db_path) as second:
            intent(first, client_order_id="uw-1", minutes=0)
            intent(second, client_order_id="uw-2", symbol="XLF", minutes=1)
            first.mark_status("uw-2", OrderStatus.FILLED, spreads_filled=SPREADS, at=at(2))
            second.mark_status("uw-1", OrderStatus.FILLED, spreads_filled=SPREADS, at=at(3))
            assert first.unreconciled_orders() == ()
            assert len(second.order_history()) == 2

    def test_an_idempotent_retry_across_connections_creates_one_order(self, db_path: Path) -> None:
        # The realistic version of this is a restart mid-retry: a new process,
        # a new connection, the same client_order_id.
        with Journal(db_path) as first, Journal(db_path) as second:
            intent(first, client_order_id="uw-1")
            intent(second, client_order_id="uw-1")
            assert len(first.order_history()) == 1

    def test_a_fill_delivered_to_two_connections_lands_once(self, db_path: Path) -> None:
        # The stream and the reconciling poll can be handled by different
        # connections. The execution id is what stops the double count.
        with Journal(db_path) as streamer, Journal(db_path) as poller:
            intent(streamer, client_order_id="uw-1", spreads=2)
            assert fill(streamer, spreads=2, source=FillSource.STREAM) is True
            assert fill(poller, spreads=2, source=FillSource.REST) is False
            assert len(poller.spread_fills_for("uw-1")) == 1
            assert poller.unconfirmed_fills() == ()
