"""Shadow P&L tests.

The one property that matters: the shadow number can only ever be worse than
the official one, never better. A conservative series that occasionally
flatters is not conservative, it is just noisy — and the whole reason this
exists is that the official figure comes from a simulator whose fill model is
undocumented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from underwriter.journal import IntentLeg, Journal, OrderRecord, OrderStatus
from underwriter.shadow import (
    EXECUTION_HAIRCUT_PER_SPREAD,
    ShadowFill,
    _limit_from,
    realised,
    shadow_price,
)

NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


def order(limit: object) -> OrderRecord:
    return OrderRecord(
        client_order_id="uw-A",
        cycle_id="c1",
        symbol="XLE",
        intent_at=NOW,
        payload={"limit_price": limit},
        status=OrderStatus.FILLED,
        spreads_ordered=1.0,
        spreads_filled=1.0,
        net_price_per_spread=-1.20,
        broker_order_id="b",
        submitted_at=None,
        status_at=None,
        reconciled_at=None,
        detail="",
    )


class TestNeverBetterThanWeAsked:
    """A paper fill that beats our own limit is the simulator being generous,
    not the market. Our limit already assumed crossing half of each leg's
    spread, so it is the most optimistic price we are willing to claim."""

    @pytest.mark.parametrize(
        ("actual", "limit", "expected"),
        [
            (-1.30, -1.20, -1.20),  # opened better than asked -> clipped
            (-1.20, -1.20, -1.20),  # opened at our limit      -> kept
            (-1.10, -1.20, -1.10),  # opened worse             -> kept, it is real
            (0.30, 0.40, 0.40),  # closed cheaper than asked -> clipped
            (0.40, 0.40, 0.40),
            (0.50, 0.40, 0.50),  # closed dearer            -> kept
        ],
    )
    def test_the_worse_price_wins(self, actual: float, limit: float, expected: float) -> None:
        assert shadow_price(actual, limit) == pytest.approx(expected)

    def test_one_rule_covers_both_directions(self) -> None:
        # Because a credit is negative and a debit positive, "worse for us" is
        # always "more positive" -- so this needs no branch on direction, and
        # cannot be got backwards for one of them.
        assert shadow_price(-1.30, -1.20) == -1.20
        assert shadow_price(0.30, 0.40) == 0.40

    def test_an_unknown_limit_keeps_the_actual_price(self) -> None:
        # A broker-initiated fill has no order of ours behind it. Inventing a
        # limit would manufacture the conservatism this module measures.
        assert shadow_price(0.30, None) == 0.30


class TestReadingTheLimit:
    def test_reads_a_signed_limit_from_the_payload(self) -> None:
        assert _limit_from(order("-1.20")) == pytest.approx(-1.20)

    def test_a_missing_order_has_no_limit(self) -> None:
        assert _limit_from(None) is None

    @pytest.mark.parametrize("bad", [None, "", "n/a", float("nan"), float("inf")])
    def test_an_unreadable_limit_is_none(self, bad: object) -> None:
        assert _limit_from(order(bad)) is None

    def test_a_bool_is_refused_rather_than_read_as_one(self) -> None:
        # bool subclasses int, so True would otherwise become a $1.00 limit --
        # a plausible-looking number assembled out of a flag.
        assert _limit_from(order(True)) is None


class TestRealised:
    def _round_trip(self, open_price: float, close_price: float, spreads: float = 1.0):  # type: ignore[no-untyped-def]
        return [
            ShadowFill("f1", "XLE", spreads, open_price, open_price, open_price),
            ShadowFill("f2", "XLE", spreads, close_price, close_price, close_price),
        ]

    def test_a_winning_round_trip(self) -> None:
        # Collected 1.20, closed at 0.40: 0.80 per spread, less the haircut.
        pnl = realised(self._round_trip(-1.20, 0.40), haircut=0.0)
        assert pnl == pytest.approx(80.0)

    def test_a_losing_round_trip_is_negative(self) -> None:
        assert realised(self._round_trip(-1.20, 3.00), haircut=0.0) < 0

    def test_a_debit_structure_is_not_inverted(self) -> None:
        # The negated-sum form applies the sign convention once to both sides.
        # A credit-minus-debit form gets this case backwards, and the error
        # looks like an unlucky trade rather than a bug.
        assert realised(self._round_trip(2.00, -3.50), haircut=0.0) == pytest.approx(150.0)

    def test_the_haircut_is_always_against_us(self) -> None:
        no_fee = realised(self._round_trip(-1.20, 0.40), haircut=0.0)
        with_fee = realised(self._round_trip(-1.20, 0.40))
        assert with_fee < no_fee

    def test_the_haircut_scales_linearly_with_size(self) -> None:
        # Fees are charged per spread, so five times the position pays five
        # times the fee AND earns five times the gross -- the two scale
        # identically and the net is exactly proportional. I first asserted
        # that a larger position does proportionally worse, which would only
        # hold for a fixed per-order cost; it is not what this models.
        one = realised(self._round_trip(-1.20, 0.40, spreads=1.0))
        five = realised(self._round_trip(-1.20, 0.40, spreads=5.0))
        assert five == pytest.approx(one * 5)

    def test_an_expiring_position_keeps_the_credit(self) -> None:
        # A spread that expires worthless has one fill and no close.
        opened = [ShadowFill("f1", "XLE", 1.0, -1.20, -1.20, -1.20)]
        assert realised(opened, haircut=0.0) == pytest.approx(120.0)

    def test_no_fills_is_no_pnl(self) -> None:
        assert realised([]) == 0.0


class TestShadowIsNeverRosier:
    """The governing property, stated as a test."""

    @pytest.mark.parametrize(
        ("actual_open", "actual_close"),
        [(-1.30, 0.30), (-1.20, 0.40), (-1.50, 0.10), (-0.90, 0.85)],
    )
    def test_shadow_never_exceeds_the_official_figure(
        self, actual_open: float, actual_close: float
    ) -> None:
        limit_open, limit_close = -1.20, 0.40
        official = -(actual_open + actual_close) * 100
        fills = [
            ShadowFill(
                "f1", "X", 1.0, actual_open, limit_open, shadow_price(actual_open, limit_open)
            ),
            ShadowFill(
                "f2", "X", 1.0, actual_close, limit_close, shadow_price(actual_close, limit_close)
            ),
        ]
        assert realised(fills) <= official + 1e-9

    def test_the_give_up_is_reported_per_fill(self) -> None:
        generous = ShadowFill("f1", "X", 2.0, -1.30, -1.20, -1.20)
        assert generous.improved_by_the_simulator
        # Ten cents a spread, two spreads, times the multiplier.
        assert generous.give_up_usd == pytest.approx(20.0)

    def test_a_fill_at_our_limit_gives_up_nothing(self) -> None:
        honest = ShadowFill("f1", "X", 2.0, -1.20, -1.20, -1.20)
        assert not honest.improved_by_the_simulator
        assert honest.give_up_usd == pytest.approx(0.0)


class TestAgainstTheJournal:
    def test_an_empty_day_is_complete_and_flat(self) -> None:
        from underwriter.shadow import shadow_for_day

        j = Journal(":memory:")
        result = shadow_for_day(j, date(2026, 8, 31))
        assert result.realised_usd == 0.0
        assert result.complete
        assert result.fills == ()
        j.close()

    def test_the_haircut_constant_is_deliberately_generous(self) -> None:
        # Better too large than too small: a haircut that understates costs
        # implies a precision this series does not have.
        assert EXECUTION_HAIRCUT_PER_SPREAD >= 0.01


def _filled_parent(journal: Journal, *, spreads: float = 1.0) -> None:
    journal.record_intent(
        client_order_id="uw-open-XLE-test",
        cycle_id="test",
        symbol="XLE",
        spreads=spreads,
        payload={"limit_price": "-0.50"},
        legs=[
            IntentLeg("XLE260911P00097000", "sell", 1, "sell_to_open"),
            IntentLeg("XLE260911P00095000", "buy", 1, "buy_to_open"),
        ],
        at=NOW,
    )
    journal.mark_status(
        "uw-open-XLE-test",
        OrderStatus.FILLED,
        spreads_filled=spreads,
        net_price_per_spread=-0.60,
        at=NOW,
    )


def test_a_filled_parent_without_events_is_not_a_complete_zero_shadow() -> None:
    from underwriter.journal import PnlSource
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        _filled_parent(journal)
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=12, at=NOW)
        result = shadow_for_day(journal, NOW.date())
        assert not result.complete
        assert result.missing_order_fills == 1
        assert result.fills == ()


@pytest.mark.parametrize("covered", [1.0, 2.0, 3.0])
def test_cumulative_parent_requires_full_confirmed_event_quantities(covered: float) -> None:
    from underwriter.journal import FillSource, PnlSource
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        _filled_parent(journal, spreads=3)
        journal.record_spread_fill(
            fill_id="execution-1",
            client_order_id="uw-open-XLE-test",
            symbol="XLE",
            spreads=covered,
            net_price_per_spread=-0.60,
            occurred_at=NOW,
            source=FillSource.REST,
            at=NOW,
        )
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0, at=NOW)
        result = shadow_for_day(journal, NOW.date())
        assert result.complete is (covered == 3)
        assert result.missing_order_fills == (0 if covered == 3 else 1)


def test_open_premium_is_not_realised_again_and_haircut_is_dollars_per_spread() -> None:
    from underwriter.journal import FillSource, PnlSource
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        _filled_parent(journal)
        journal.record_spread_fill(
            fill_id="execution-open",
            client_order_id="uw-open-XLE-test",
            symbol="XLE",
            spreads=1,
            net_price_per_spread=-0.60,
            occurred_at=NOW,
            source=FillSource.REST,
            at=NOW,
        )
        # The broker has $20 unrealised, zero realised. The simulator gave
        # $10 more opening premium than our limit; surrender only that gain.
        journal.record_pnl(
            source=PnlSource.OFFICIAL, realised_pnl=0, unrealised_pnl=20, at=NOW
        )
        result = shadow_for_day(journal, NOW.date())
        assert result.complete
        assert result.realised_usd == pytest.approx(-10.05)
        assert result.haircut_usd == pytest.approx(0.05)
        assert result.realised_usd + 20 == pytest.approx(9.95)
    fills = [ShadowFill("open", "XLE", 1, -0.5, -0.5, -0.5)]
    assert realised(fills, haircut=0) - realised(fills) == pytest.approx(0.05)


def test_filled_parent_requires_an_official_mark_at_or_after_the_observation() -> None:
    from datetime import timedelta

    from underwriter.journal import PnlSource, RecoveryGap
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        _filled_parent(journal)
        recovery = journal.recover(now=NOW)
        assert recovery.realised_pnl_today is None
        assert RecoveryGap.REALISED_PNL_UNKNOWN in recovery.gaps
        assert shadow_for_day(journal, NOW.date()).official_missing
        journal.record_pnl(
            source=PnlSource.OFFICIAL, realised_pnl=-1600, at=NOW - timedelta(seconds=1)
        )
        assert journal.recover(now=NOW).realised_pnl_today is None
        assert shadow_for_day(journal, NOW.date()).official_missing
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-1600, at=NOW)
        assert journal.recover(now=NOW).realised_pnl_today == -1600
        assert not shadow_for_day(journal, NOW.date()).official_missing


def test_cycle_does_not_publish_shadow_for_uncovered_parent_fills() -> None:
    from tests.test_cycle import DAY, build, passing_preflight
    from underwriter.journal import PnlSource

    with Journal() as journal:
        cycle, *_ = build(journal)
        report = cycle.run(preflight=passing_preflight())
        assert len(report.opened) == 1
        assert journal.latest_pnl(trading_day=DAY, source=PnlSource.OFFICIAL) is not None
        assert journal.latest_pnl(trading_day=DAY, source=PnlSource.SHADOW) is None
        assert any("No shadow P&L snapshot" in note for note in report.notes)


@pytest.mark.parametrize("status", [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED])
@pytest.mark.parametrize("reported_quantity", [None, 1.0])
def test_filled_status_with_missing_quantity_or_price_requires_accounting_evidence(
    status: OrderStatus, reported_quantity: float | None
) -> None:
    from underwriter.journal import FillSource, PnlSource, RecoveryGap
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        journal.record_intent(
            client_order_id="uw-open-XLE-incomplete",
            cycle_id="incomplete",
            symbol="XLE",
            spreads=1,
            payload={"limit_price": "-0.50"},
            legs=[
                IntentLeg("XLE260911P00097000", "sell", 1, "sell_to_open"),
                IntentLeg("XLE260911P00095000", "buy", 1, "buy_to_open"),
            ],
            at=NOW,
        )
        journal.mark_status(
            "uw-open-XLE-incomplete",
            status,
            spreads_filled=reported_quantity,
            at=NOW,
        )
        observed = journal.filled_order_observations(NOW.date())
        assert len(observed) == 1
        assert observed[0].net_price_per_spread is None
        recovery = journal.recover(now=NOW)
        assert recovery.realised_pnl_today is None
        assert RecoveryGap.REALISED_PNL_UNKNOWN in recovery.gaps
        result = shadow_for_day(journal, NOW.date())
        assert not result.complete
        assert result.missing_order_fills == 1
        assert result.official_missing

        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=-10, at=NOW)
        if reported_quantity is not None:
            journal.record_spread_fill(
                fill_id="confirmed-incomplete-parent",
                client_order_id="uw-open-XLE-incomplete",
                symbol="XLE",
                spreads=reported_quantity,
                net_price_per_spread=-0.50,
                occurred_at=NOW,
                source=FillSource.REST,
                at=NOW,
            )
        result = shadow_for_day(journal, NOW.date())
        assert not result.official_missing
        assert not result.complete
        assert result.missing_order_fills == 1


def test_confirmed_events_exceeding_parent_filled_quantity_are_incomplete() -> None:
    from underwriter.journal import FillSource, PnlSource
    from underwriter.shadow import shadow_for_day

    with Journal() as journal:
        journal.record_intent(
            client_order_id="uw-open-XLE-disagreement",
            cycle_id="disagreement",
            symbol="XLE",
            spreads=3,
            payload={"limit_price": "-0.50"},
            legs=[
                IntentLeg("XLE260911P00097000", "sell", 1, "sell_to_open"),
                IntentLeg("XLE260911P00095000", "buy", 1, "buy_to_open"),
            ],
            at=NOW,
        )
        journal.mark_status(
            "uw-open-XLE-disagreement",
            OrderStatus.PARTIALLY_FILLED,
            spreads_filled=1,
            net_price_per_spread=-0.50,
            at=NOW,
        )
        journal.record_spread_fill(
            fill_id="execution-disagreement",
            client_order_id="uw-open-XLE-disagreement",
            symbol="XLE",
            spreads=2,
            net_price_per_spread=-0.50,
            occurred_at=NOW,
            source=FillSource.REST,
            at=NOW,
        )
        journal.record_pnl(source=PnlSource.OFFICIAL, realised_pnl=0, at=NOW)
        result = shadow_for_day(journal, NOW.date())
        assert not result.complete
        assert result.missing_order_fills == 1
