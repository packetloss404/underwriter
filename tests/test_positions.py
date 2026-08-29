"""Spread reassembly tests.

The broker reports contracts; we hold spreads. Three things depend on this
mapping being right -- the risk engine's open risk and net delta, the snapshot
diff that is our only same-day assignment signal, and every exit trigger -- so
the tests weight heavily toward the ways a contract can fail to be attributed.

The governing rule: a contract that cannot be paired is returned as an orphan,
never dropped. A dropped leg is a position we hold and do not know about.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from underwriter.journal import IntentLeg, OrderRecord, OrderStatus
from underwriter.positions import (
    Orphan,
    RawOptionPosition,
    Unpairable,
    reassemble_spreads,
)

SHORT_A = "SPY260911P00760000"
LONG_A = "SPY260911P00755000"
SHORT_B = "SPY260911P00750000"
LONG_B = "SPY260911P00745000"


def order(cid: str, spreads: float = 1.0, net: float = -0.42) -> OrderRecord:
    return OrderRecord(
        client_order_id=cid,
        cycle_id="c1",
        symbol="SPY",
        intent_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        payload={},
        status=OrderStatus.FILLED,
        spreads_ordered=spreads,
        spreads_filled=spreads,
        net_price_per_spread=net,
        broker_order_id="b",
        submitted_at=None,
        status_at=None,
        reconciled_at=None,
        detail="",
    )


def legs(short: str, long_: str) -> list[IntentLeg]:
    return [
        IntentLeg(short, "sell", 1, "sell_to_open"),
        IntentLeg(long_, "buy", 1, "buy_to_open"),
    ]


def one_spread(
    spreads: float = 1.0, net: float = -0.42
) -> tuple[list[RawOptionPosition], dict[str, list[OrderRecord]], dict[str, list[IntentLeg]]]:
    o = order("uw-A", spreads, net)
    ls = {"uw-A": legs(SHORT_A, LONG_A)}
    holding = {SHORT_A: [o], LONG_A: [o]}
    raw = [
        RawOptionPosition(SHORT_A, -spreads),
        RawOptionPosition(LONG_A, spreads),
    ]
    return raw, holding, ls


class TestPairing:
    def test_a_two_leg_spread_becomes_one_position(self) -> None:
        raw, holding, ls = one_spread()
        records, orphans = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert len(records) == 1
        assert orphans == []
        assert records[0].client_order_id == "uw-A"

    def test_adjacent_spreads_are_not_confused(self) -> None:
        # The reason pairing goes through the journal rather than geometry:
        # 760/755 and 750/745 are indistinguishable from 760/745 and 750/755
        # if you only look at strikes.
        a, b = order("uw-A", 2.0, -0.42), order("uw-B", 1.0, -0.31)
        ls = {"uw-A": legs(SHORT_A, LONG_A), "uw-B": legs(SHORT_B, LONG_B)}
        holding = {
            SHORT_A: [a],
            LONG_A: [a],
            SHORT_B: [b],
            LONG_B: [b],
        }
        raw = [
            RawOptionPosition(SHORT_A, -2.0),
            RawOptionPosition(LONG_A, 2.0),
            RawOptionPosition(SHORT_B, -1.0),
            RawOptionPosition(LONG_B, 1.0),
        ]
        records, orphans = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert orphans == []
        by_order = {r.client_order_id: r for r in records}
        assert by_order["uw-A"].spreads == 2.0
        assert by_order["uw-B"].spreads == 1.0
        assert "760000/SPY260911P00755000" in by_order["uw-A"].detail

    def test_each_contract_is_consumed_once(self) -> None:
        raw, holding, ls = one_spread()
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert len(records) == 1

    def test_partial_close_uses_the_smaller_side(self) -> None:
        # Half the position closed leaves an unbalanced pair; the spread count
        # is what we still hold intact.
        o = order("uw-A", 3.0)
        ls = {"uw-A": legs(SHORT_A, LONG_A)}
        holding = {SHORT_A: [o], LONG_A: [o]}
        raw = [RawOptionPosition(SHORT_A, -1.0), RawOptionPosition(LONG_A, 3.0)]
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert records[0].spreads == 1.0


class TestOrphans:
    def _orphan(self, orphans: list[Orphan], symbol: str) -> Orphan:
        return next(o for o in orphans if o.position.symbol == symbol)

    def test_unjournalled_contract_is_an_orphan_not_a_drop(self) -> None:
        raw = [RawOptionPosition("SPY260911P00700000", 1.0)]
        records, orphans = reassemble_spreads(raw, orders_holding={}, legs_of={})
        assert records == []
        assert self._orphan(orphans, "SPY260911P00700000").reason is (
            Unpairable.NO_JOURNALLED_ORDER
        )

    def test_surviving_wing_after_assignment_is_an_orphan(self) -> None:
        # The commonest real case: the short leg was assigned away overnight
        # and we still hold the long wing. It carries risk and must be visible.
        o = order("uw-A")
        ls = {"uw-A": legs(SHORT_A, LONG_A)}
        holding = {SHORT_A: [o], LONG_A: [o]}
        raw = [RawOptionPosition(LONG_A, 1.0)]
        records, orphans = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert records == []
        orphan = self._orphan(orphans, LONG_A)
        assert orphan.reason is Unpairable.NO_OPPOSING_LEG
        assert "still ours" in orphan.detail

    def test_two_legs_on_the_same_side_do_not_pair(self) -> None:
        # Both long is not a spread. Pairing them would invent a structure.
        o = order("uw-A")
        ls = {"uw-A": legs(SHORT_A, LONG_A)}
        holding = {SHORT_A: [o], LONG_A: [o]}
        raw = [RawOptionPosition(SHORT_A, 1.0), RawOptionPosition(LONG_A, 1.0)]
        records, orphans = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert records == []
        assert len(orphans) == 2

    def test_unparseable_symbol_is_an_orphan(self) -> None:
        raw = [RawOptionPosition("NOT-AN-OCC-SYMBOL", 1.0)]
        _, orphans = reassemble_spreads(raw, orders_holding={}, legs_of={})
        assert orphans[0].reason is Unpairable.UNPARSEABLE_SYMBOL

    def test_zero_quantity_is_an_orphan(self) -> None:
        raw = [RawOptionPosition(SHORT_A, 0.0)]
        _, orphans = reassemble_spreads(raw, orders_holding={}, legs_of={})
        assert orphans[0].reason is Unpairable.ZERO_QUANTITY

    def test_order_without_legs_is_reported_distinctly(self) -> None:
        # Distinguishes "we never recorded the legs" from "the other leg is
        # gone" -- different problems needing different responses.
        o = order("uw-A")
        raw = [RawOptionPosition(SHORT_A, -1.0)]
        _, orphans = reassemble_spreads(raw, orders_holding={SHORT_A: [o]}, legs_of={"uw-A": []})
        assert orphans[0].reason is Unpairable.ORDER_LEGS_MISSING

    def test_every_input_contract_is_accounted_for(self) -> None:
        raw, holding, ls = one_spread()
        raw = [*raw, RawOptionPosition("SPY260911P00700000", 1.0)]
        records, orphans = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        # Two consumed into one record, one orphaned. Nothing vanishes.
        assert len(records) * 2 + len(orphans) == len(raw)


class TestEconomics:
    def test_max_loss_is_a_position_total_not_per_spread(self) -> None:
        # A per-spread figure fed into the aggregate cap understates open risk
        # by the contract count, which is the direction that lets the book grow
        # past its limit while reporting healthy.
        one, holding, ls = one_spread(spreads=1.0, net=-0.42)
        two, holding2, ls2 = one_spread(spreads=2.0, net=-0.42)
        r1, _ = reassemble_spreads(one, orders_holding=holding, legs_of=ls)
        r2, _ = reassemble_spreads(two, orders_holding=holding2, legs_of=ls2)
        assert r2[0].max_loss == pytest.approx(r1[0].max_loss * 2)

    def test_max_loss_is_width_minus_credit(self) -> None:
        raw, holding, ls = one_spread(spreads=1.0, net=-0.42)
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert records[0].max_loss == pytest.approx((5.0 - 0.42) * 100)

    def test_credit_magnitude_is_taken_from_the_signed_price(self) -> None:
        # net_price_per_spread is negative for a credit; max loss must use its
        # magnitude, not the signed value.
        raw, holding, ls = one_spread(net=-0.42)
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls)
        assert records[0].max_loss < 5.0 * 100

    def test_net_delta_is_positive_for_a_put_credit_spread(self) -> None:
        raw, holding, ls = one_spread()
        records, _ = reassemble_spreads(
            raw,
            orders_holding=holding,
            legs_of=ls,
            deltas={SHORT_A: -0.22, LONG_A: -0.15},
        )
        assert records[0].net_delta == pytest.approx(7.0)

    def test_missing_wing_delta_overstates_rather_than_understates(self) -> None:
        raw, holding, ls = one_spread()
        both, _ = reassemble_spreads(
            raw,
            orders_holding=holding,
            legs_of=ls,
            deltas={SHORT_A: -0.22, LONG_A: -0.15},
        )
        wing_missing, _ = reassemble_spreads(
            raw,
            orders_holding=holding,
            legs_of=ls,
            deltas={SHORT_A: -0.22},
        )
        assert wing_missing[0].net_delta > both[0].net_delta
        assert "wing-missing" in wing_missing[0].detail

    def test_unknown_short_delta_is_recorded_not_guessed(self) -> None:
        raw, holding, ls = one_spread()
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls, deltas={})
        assert records[0].net_delta == 0.0
        assert "delta:unknown" in records[0].detail

    def test_unrealised_pnl_uses_our_quotes(self) -> None:
        raw, holding, ls = one_spread(net=-0.42)
        records, _ = reassemble_spreads(
            raw,
            orders_holding=holding,
            legs_of=ls,
            quotes={SHORT_A: 0.20, LONG_A: 0.10},
        )
        # Collected 0.42, costs 0.10 to close, so 0.32 per spread.
        assert records[0].unrealised_pnl == pytest.approx(32.0)

    def test_a_losing_position_reports_negative_unrealised(self) -> None:
        raw, holding, ls = one_spread(net=-0.42)
        records, _ = reassemble_spreads(
            raw,
            orders_holding=holding,
            legs_of=ls,
            quotes={SHORT_A: 1.50, LONG_A: 0.40},
        )
        assert records[0].unrealised_pnl < 0

    def test_missing_quotes_report_unknown_rather_than_zero_profit(self) -> None:
        raw, holding, ls = one_spread()
        records, _ = reassemble_spreads(raw, orders_holding=holding, legs_of=ls, quotes={})
        assert "pnl:unknown" in records[0].detail
