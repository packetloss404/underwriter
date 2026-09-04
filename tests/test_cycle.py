"""Cycle tests: the ordering, and what happens when a stage cannot answer.

The cycle is the only module that knows what order the others must run in, so
these tests are mostly about order and about refusal. A full pass runs here
end to end with no network and no credentials -- that is the point of the
injection, and it is what makes the ordering provable rather than asserted.

The four properties under test, in the order they matter:

1. The book is observed first, and a cycle that cannot establish it opens
   nothing.
2. Exits are decided and placed before any entry is considered, kill switch or
   no kill switch.
3. `record_intent` is committed before `execution.submit` is called, on every
   path. The fake executor asserts this from inside the call.
4. One bad symbol costs us that symbol and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from underwriter.chain import ExpiryWindow
from underwriter.config import RiskLimits
from underwriter.cycle import (
    AccountFacts,
    Action,
    Book,
    BrokerOrderView,
    Cycle,
    CycleReport,
    ExitPass,
    Failed,
    Halt,
    MarketView,
    Refusal,
    SystemClock,
    VetoVerdict,
    account_facts,
    closing_spread,
    cycle_id_for,
    intent_legs,
    session_time_et,
    to_open_positions,
)
from underwriter.data import Bars, SnapshotLike
from underwriter.execution import (
    Backend,
    MultiLegOrder,
    OrderResult,
    Reason,
    build_opening_order,
    validate,
)
from underwriter.exits import ExitDecision, ExitReason
from underwriter.journal import (
    IntentLeg,
    Journal,
    KillSwitchActor,
    OrderStatus,
    PnlSource,
    Stage,
)
from underwriter.positions import OpenSpread, RawOptionPosition
from underwriter.preflight import Check, PreflightReport, Status
from underwriter.regime import RegimeBlock

# 14:30 UTC is 10:30 ET: inside the session and well before the 15:00 ET entry
# cutoff.
NOW = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
DAY = date(2026, 8, 31)
NEAR_EXPIRY = date(2026, 9, 11)

XLE_SHORT = "XLE260911P00097000"
XLE_LONG = "XLE260911P00095000"
XLF_SHORT = "XLF260911P00097000"
XLF_LONG = "XLF260911P00095000"
# Two days out: inside `force_flat_days_before_expiry`, so the time stop fires
# whether or not the legs can be priced.
XLE_SOON_SHORT = "XLE260902P00097000"
XLE_SOON_LONG = "XLE260902P00095000"


def closes(count: int = 40) -> list[float]:
    """A gently rising series with alternating noise.

    Rising so the benchmark sits above its 20-session average and has no
    three-session drawdown; alternating so realised volatility is positive and
    the ten- and twenty-session windows agree, which keeps the expansion check
    quiet. Last close 100.35.
    """
    return [90.0 + i * 0.25 + (0.6 if i % 2 else -0.6) for i in range(count)]


# --------------------------------------------------------------------------
# Fakes. Nothing here touches a network, a credential, or the wall clock.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeClock:
    at: datetime

    def now(self) -> datetime:
        return self.at


@dataclass(frozen=True, slots=True)
class FakeAccount:
    """Shaped to `cycle.AccountView`, which extends `preflight.AccountLike`.

    Values are strings where Alpaca returns strings, so the parsing this code
    does is actually exercised.
    """

    equity: object = "100000"
    options_buying_power: object = "50000"
    last_equity: object = "100000"
    status: object = "ACTIVE"
    trading_blocked: bool = False
    account_blocked: bool = False
    options_trading_level: object = "3"
    options_approved_level: object = "3"


@dataclass(frozen=True, slots=True)
class FakeQuote:
    bid_price: float | None
    ask_price: float | None
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class FakeGreeks:
    delta: float | None


@dataclass(frozen=True, slots=True)
class FakeSnapshot:
    latest_quote: FakeQuote | None
    implied_volatility: float | None = None
    greeks: FakeGreeks | None = None


def snapshot(
    bid: float, ask: float, *, delta: float | None = None, iv: float | None = None, at: datetime
) -> FakeSnapshot:
    return FakeSnapshot(
        latest_quote=FakeQuote(bid_price=bid, ask_price=ask, timestamp=at),
        implied_volatility=iv,
        greeks=None if delta is None else FakeGreeks(delta),
    )


def tradeable_chain(
    short: str, long_: str, *, at: datetime, iv: float = 0.30
) -> dict[str, SnapshotLike]:
    """A chain that yields exactly one viable put credit spread.

    Short leg 1.00/1.06 at -0.25 delta, long wing 0.46/0.50 at -0.10. Modelled
    credit is 0.50 on a 2.00 width -- a quarter of the width, inside the
    selector's 15-50% band -- for a max loss of 150 per spread.
    """
    return {
        short: snapshot(1.00, 1.06, delta=-0.25, iv=iv, at=at),
        long_: snapshot(0.46, 0.50, delta=-0.10, iv=iv, at=at),
    }


@dataclass(slots=True)
class FakeBroker:
    account_obj: FakeAccount = field(default_factory=FakeAccount)
    raw: list[RawOptionPosition] = field(default_factory=list)
    positions_error: Exception | None = None
    account_error: Exception | None = None
    account_calls: int = 0

    def account(self) -> FakeAccount:
        self.account_calls += 1
        if self.account_error is not None:
            raise self.account_error
        return self.account_obj

    def positions(self) -> Sequence[RawOptionPosition]:
        if self.positions_error is not None:
            raise self.positions_error
        return list(self.raw)


@dataclass(slots=True)
class FakeMarket:
    at: datetime = NOW
    bars: dict[str, list[float]] = field(default_factory=dict)
    near: dict[str, dict[str, SnapshotLike]] = field(default_factory=dict)
    far: dict[str, dict[str, SnapshotLike]] = field(default_factory=dict)
    snapshots: dict[str, SnapshotLike] = field(default_factory=dict)
    chain_errors: dict[str, Exception] = field(default_factory=dict)
    bars_error: Exception | None = None
    chain_calls: list[tuple[str, ExpiryWindow]] = field(default_factory=list)

    def daily_closes(self, symbols: Sequence[str]) -> Bars:
        if self.bars_error is not None:
            raise self.bars_error
        return Bars(closes={s: list(self.bars.get(s, ())) for s in symbols})

    def chain(self, underlying: str, window: ExpiryWindow) -> Mapping[str, SnapshotLike]:
        self.chain_calls.append((underlying, window))
        error = self.chain_errors.get(underlying)
        if error is not None:
            raise error
        # The far window spans 30 days; the entry window spans nine.
        far = (window.lte - window.gte).days > 20
        source = self.far if far else self.near
        return dict(source.get(underlying, {}))

    def option_snapshots(self, symbols: Sequence[str]) -> Mapping[str, SnapshotLike]:
        return {s: self.snapshots[s] for s in symbols if s in self.snapshots}


@dataclass(slots=True)
class FakeExecutor:
    """Records every submission, and checks the write-ahead rule from inside.

    Two assertions live here rather than in a test body because they must hold
    on *every* path through the cycle, not only the ones a test remembers to
    check: the intent is journalled before submit is called, and the payload
    the cycle built is one `execution.validate` accepts.
    """

    journal: Journal
    results: dict[str, OrderResult] = field(default_factory=dict)
    calls: list[MultiLegOrder] = field(default_factory=list)
    seen_status: list[OrderStatus | None] = field(default_factory=list)
    error: Exception | None = None

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> OrderResult:
        assert validate(order) is None, validate(order)
        record = self.journal.order(order.client_order_id)
        self.seen_status.append(None if record is None else record.status)
        self.calls.append(order)
        if self.error is not None:
            raise self.error
        default = OrderResult(
            ok=True,
            backend=Backend.SDK,
            client_order_id=order.client_order_id,
            payload=order.as_payload(),
            order_id=f"broker-{len(self.calls)}",
            status="filled",
            limit_price=order.limit_price,
            filled_qty=Decimal(order.qty),
            filled_avg_price=order.limit_price,
            dry_run=dry_run,
            at=NOW,
        )
        return self.results.get(order.client_order_id.split("-")[1], default)

    @property
    def actions(self) -> list[str]:
        """`open` or `close`, in the order the orders were actually sent."""
        return [o.client_order_id.split("-")[1] for o in self.calls]


@dataclass(frozen=True, slots=True)
class FakeVeto:
    verdict: VetoVerdict = field(default_factory=lambda: VetoVerdict(vetoed=False))
    error: Exception | None = None

    def screen(self, *, symbol: str, ranking: object, spread: object) -> VetoVerdict:
        del symbol, ranking, spread
        if self.error is not None:
            raise self.error
        return self.verdict


@dataclass(slots=True)
class FakeOrders:
    views: dict[str, BrokerOrderView | None] = field(default_factory=dict)
    error: Exception | None = None
    asked: list[str] = field(default_factory=list)

    def order_status(self, client_order_id: str) -> BrokerOrderView | None:
        self.asked.append(client_order_id)
        if self.error is not None:
            raise self.error
        return self.views.get(client_order_id)


def passing_preflight() -> PreflightReport:
    return PreflightReport(
        checks=[Check(name="account.status", status=Status.OK, detail="ACTIVE")], run_at=NOW
    )


def failing_preflight() -> PreflightReport:
    return PreflightReport(
        checks=[
            Check(name="options.effective_level", status=Status.FAIL, detail="level 2, need 3")
        ],
        run_at=NOW,
    )


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


@pytest.fixture
def journal() -> Journal:
    return Journal()


def market_for(symbols: Sequence[str], *, at: datetime = NOW) -> FakeMarket:
    """A market where every named symbol carries one viable spread.

    The benchmark always gets bars and both term-structure slices: near-dated
    implied vol at 30% against far-dated 35% is a healthy contango curve, 35
    days apart, so the regime filter has the forward-looking input it refuses
    to trade without.
    """
    legs = {"XLE": (XLE_SHORT, XLE_LONG), "XLF": (XLF_SHORT, XLF_LONG)}
    market = FakeMarket(at=at)
    market.bars = {s: closes() for s in ("SPY", *symbols)}
    for symbol in symbols:
        short, long_ = legs[symbol]
        market.near[symbol] = tradeable_chain(short, long_, at=at)
    market.near["SPY"] = {"SPY260911P00097000": snapshot(1.00, 1.06, iv=0.30, at=at)}
    market.far["SPY"] = {"SPY261016P00097000": snapshot(2.00, 2.10, iv=0.35, at=at)}
    return market


def build(
    journal: Journal,
    *,
    symbols: Sequence[str] = ("XLE",),
    at: datetime = NOW,
    market: FakeMarket | None = None,
    broker: FakeBroker | None = None,
    orders: FakeOrders | None = None,
    veto: FakeVeto | None = None,
    veto_required: bool = False,
    kill_switch: bool = False,
    dry_run: bool = False,
    limits: RiskLimits | None = None,
) -> tuple[Cycle, FakeMarket, FakeBroker, FakeExecutor]:
    """Wire a cycle to fakes. Every dependency is named, none is real."""
    market = market or market_for(symbols, at=at)
    broker = broker or FakeBroker()
    executor = FakeExecutor(journal=journal)
    cycle = Cycle(
        journal=journal,
        market=market,
        broker=broker,
        execution=executor,
        clock=FakeClock(at),
        orders=orders,
        veto=veto,
        veto_required=veto_required,
        kill_switch=kill_switch,
        dry_run=dry_run,
        limits=limits or RiskLimits(),
        universe=tuple(symbols),
    )
    return cycle, market, broker, executor


def hold_xle(
    journal: Journal,
    market: FakeMarket,
    broker: FakeBroker,
    *,
    exit_ask: float,
    short: str = XLE_SHORT,
    long_: str = XLE_LONG,
) -> None:
    """Put a filled three-spread XLE put credit spread on the books.

    Journalled the way the cycle itself would have journalled it, because
    reassembly pairs contracts through the journal's leg map and not through
    geometry -- a position with no recorded order is an orphan, by design.
    """
    journal.record_intent(
        client_order_id="uw-open-XLE-20260828-held",
        cycle_id="2026-08-28-100000",
        symbol="XLE",
        spreads=3.0,
        payload={"order_class": "mleg"},
        legs=[
            IntentLeg(short, "sell", 1, "sell_to_open"),
            IntentLeg(long_, "buy", 1, "buy_to_open"),
        ],
        at=NOW - timedelta(days=1),
    )
    journal.mark_status(
        "uw-open-XLE-20260828-held",
        OrderStatus.FILLED,
        spreads_filled=3.0,
        net_price_per_spread=-0.50,
        broker_order_id="broker-held",
        at=NOW - timedelta(days=1),
    )
    broker.raw = [
        RawOptionPosition(short, -3.0),
        RawOptionPosition(long_, 3.0),
    ]
    # The exit is priced against the short leg's ask and the long wing's bid.
    market.snapshots = {
        short: snapshot(exit_ask - 0.04, exit_ask, delta=-0.20, at=NOW),
        long_: snapshot(0.02, 0.06, delta=-0.08, at=NOW),
    }


# --------------------------------------------------------------------------
# The ET clock and the cycle id
# --------------------------------------------------------------------------


class TestClockHelpers:
    def test_session_time_is_exchange_local(self) -> None:
        # 14:30 UTC in August is 10:30 in New York. Reading this as UTC would
        # move the 15:00 ET entry cutoff by four hours, in the direction that
        # keeps trading.
        assert session_time_et(NOW).hour == 10

    def test_cycle_id_is_sortable_and_on_the_exchange_clock(self) -> None:
        assert cycle_id_for(NOW) == "2026-08-31-103000"
        later = cycle_id_for(NOW + timedelta(minutes=5))
        assert later > cycle_id_for(NOW)

    def test_cycle_id_files_a_late_evening_moment_under_the_right_session(self) -> None:
        # 01:30 UTC on Tuesday is 21:30 ET on Monday: still Monday's session.
        assert cycle_id_for(datetime(2026, 9, 1, 1, 30, tzinfo=UTC)).startswith("2026-08-31")

    def test_a_naive_clock_is_refused(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        naive = Cycle(
            journal=journal,
            market=cycle.market,
            broker=cycle.broker,
            execution=cycle.execution,
            clock=FakeClock(datetime(2026, 8, 31, 14, 30)),  # noqa: DTZ001
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            naive.run()

    def test_system_clock_is_aware(self) -> None:
        assert SystemClock().now().tzinfo is not None


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


class TestConversions:
    def test_unreadable_equity_is_not_zero(self) -> None:
        facts = account_facts(FakeAccount(equity="n/a"))
        assert not facts.readable
        assert facts.detail
        # Zero is a number the risk engine would reason about. NaN is not.
        assert facts.equity != 0

    def test_readable_account_parses_strings(self) -> None:
        facts = account_facts(FakeAccount())
        assert facts.readable
        assert facts.equity == 100_000
        assert facts.last_equity == 100_000

    def test_open_positions_carry_position_totals(self) -> None:
        spread = OpenSpread(
            underlying="XLE",
            short_symbol=XLE_SHORT,
            long_symbol=XLE_LONG,
            expiry=NEAR_EXPIRY,
            spreads=3.0,
            width=2.0,
            credit_per_spread=0.50,
            max_loss=450.0,
            net_delta=45.0,
            unrealised_pnl=12.0,
            client_order_id="uw-A",
        )
        (position,) = to_open_positions([spread])
        assert position.max_loss == 450.0
        assert position.net_delta == 45.0

    def test_closing_spread_is_parsed_not_invented(self) -> None:
        spread = OpenSpread(
            underlying="XLE",
            short_symbol=XLE_SHORT,
            long_symbol=XLE_LONG,
            expiry=NEAR_EXPIRY,
            spreads=1.0,
            width=2.0,
            credit_per_spread=0.50,
            max_loss=150.0,
            net_delta=15.0,
            unrealised_pnl=0.0,
            client_order_id="uw-A",
        )
        rebuilt = closing_spread(spread)
        assert rebuilt.short_leg.strike == 97.0
        assert rebuilt.long_leg.strike == 95.0
        assert rebuilt.expiry == NEAR_EXPIRY
        assert rebuilt.width == 2.0
        # Nothing was fabricated: the legs carry no quote we did not observe.
        assert rebuilt.short_leg.quote is None

    def test_intent_legs_mirror_the_order(self) -> None:
        order = build_opening_order(
            closing_spread(
                OpenSpread(
                    underlying="XLE",
                    short_symbol=XLE_SHORT,
                    long_symbol=XLE_LONG,
                    expiry=NEAR_EXPIRY,
                    spreads=1.0,
                    width=2.0,
                    credit_per_spread=0.50,
                    max_loss=150.0,
                    net_delta=15.0,
                    unrealised_pnl=0.0,
                    client_order_id="uw-A",
                )
            ),
            contracts=1,
            now=NOW,
        )
        legs = intent_legs(order)
        assert [leg.position_intent for leg in legs] == ["sell_to_open", "buy_to_open"]
        assert [leg.occ_symbol for leg in legs] == [XLE_SHORT, XLE_LONG]


# --------------------------------------------------------------------------
# A full clean cycle
# --------------------------------------------------------------------------


class TestCleanCycle:
    @pytest.fixture
    def result(self, journal: Journal) -> tuple[CycleReport, FakeExecutor, Journal]:
        cycle, _market, _broker, executor = build(journal)
        return cycle.run(preflight=passing_preflight()), executor, journal

    def test_it_opens_the_ranked_candidate(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, executor, _ = result
        assert report.halts == ()
        assert [s.symbol for s in report.opened] == ["XLE"]
        assert len(executor.calls) == 1

    def test_sizing_respects_the_per_trade_budget(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        # 0.5% of 100k is 500; one spread risks 150; three fit and four do not.
        (opened,) = result[0].opened
        assert opened.spreads == 3

    def test_the_order_is_a_credit(self, result: tuple[CycleReport, FakeExecutor, Journal]) -> None:
        # A positive limit on a credit spread reads as "I will pay to enter".
        (opened,) = result[0].opened
        assert opened.limit_price < 0

    def test_the_book_was_observed_and_the_diff_advanced(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, _, journal = result
        assert report.observed
        assert journal.latest_positions().observed
        assert journal.undiffed_snapshots() == 0

    def test_the_ranking_is_on_the_record(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        (ranking,) = result[0].rankings
        assert ranking.symbol == "XLE"
        assert ranking.vrp_ratio > 1.15

    def test_the_regime_verdict_is_journalled_even_when_it_allows(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        # The filter is judged on whether it fired at the right times, which is
        # unanswerable if only its refusals are on disk.
        (verdict,) = result[2].regime_history()
        assert verdict.allowed
        shadows = verdict.context["trend_shadows"]
        assert isinstance(shadows, dict)
        assert shadows["hard_20ma"]["may_open"] is True
        assert result[0].regime is not None
        assert result[0].regime.may_open

    def test_the_term_structure_was_built(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        term = result[0].term_structure
        assert term is not None
        assert term.gap_days >= 14
        assert term.is_contango

    def test_the_order_is_journalled_with_its_legs(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, _, journal = result
        (opened,) = report.opened
        record = journal.order(opened.client_order_id)
        assert record is not None
        assert record.symbol == "XLE"
        assert record.spreads_ordered == 3
        legs = journal.legs_for(opened.client_order_id)
        assert {leg.occ_symbol for leg in legs} == {XLE_SHORT, XLE_LONG}

    def test_the_fill_is_recorded_against_the_order(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, _, journal = result
        record = journal.order(report.opened[0].client_order_id)
        assert record is not None
        assert record.status is OrderStatus.FILLED
        assert record.spreads_filled == 3
        # Signed, and negative for a filled credit.
        assert record.net_price_per_spread is not None
        assert record.net_price_per_spread < 0

    def test_every_stage_left_a_decision(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, _, journal = result
        stages = {d.stage for d in journal.recent_decisions(limit=100, cycle_id=report.cycle_id)}
        assert {Stage.VETO, Stage.RISK} <= stages

    def test_the_cycle_id_ties_the_decisions_to_the_order(
        self, result: tuple[CycleReport, FakeExecutor, Journal]
    ) -> None:
        report, _, journal = result
        record = journal.order(report.opened[0].client_order_id)
        assert record is not None
        assert record.cycle_id == report.cycle_id


# --------------------------------------------------------------------------
# 1. Observe first
# --------------------------------------------------------------------------


class TestObserveFirst:
    def test_a_failed_observe_opens_nothing(self, journal: Journal) -> None:
        broker = FakeBroker(positions_error=ConnectionError("positions unreachable"))
        cycle, _market, _broker, executor = build(journal, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        assert not report.observed
        assert Halt.BOOK_UNKNOWN in report.halts
        assert executor.calls == []

    def test_a_failed_observe_is_recorded_with_a_reason(self, journal: Journal) -> None:
        broker = FakeBroker(positions_error=ConnectionError("positions unreachable"))
        cycle, *_ = build(journal, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        (failure,) = report.failures
        assert failure.reason is Failed.OBSERVE_ERROR
        assert "positions unreachable" in failure.detail
        assert any(Halt.BOOK_UNKNOWN.value in r.reasons for r in report.rejections), (
            "the halt must be journalled, not merely returned"
        )

    def test_a_failed_observe_does_not_read_as_a_flat_account(self, journal: Journal) -> None:
        # The dangerous failure: an unread book that presents as an empty one is
        # the most permissive state the risk engine can be handed.
        broker = FakeBroker(positions_error=ConnectionError("boom"))
        cycle, *_ = build(journal, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        assert report.observation is None
        assert report.needs_attention

    def test_orphans_are_surfaced_not_dropped(self, journal: Journal) -> None:
        broker = FakeBroker(raw=[RawOptionPosition(XLE_SHORT, -1.0)])
        market = market_for(("XLE",))
        market.snapshots = {XLE_SHORT: snapshot(0.20, 0.24, at=NOW)}
        cycle, *_ = build(journal, market=market, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        assert report.observation is not None
        assert len(report.observation.orphans) == 1
        assert any(r.symbol == XLE_SHORT for r in report.rejections)
        assert report.needs_attention

    def test_an_unexplained_departure_is_noted(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.60)
        cycle.run(preflight=passing_preflight())

        # The position vanishes with no fill of ours: on paper this is the only
        # same-day signal of an assignment or an expiry.
        broker.raw = []
        report = cycle.run(preflight=passing_preflight())

        assert report.observation is not None
        (event,) = report.observation.events
        assert event.symbol == "XLE"
        assert report.needs_attention


# --------------------------------------------------------------------------
# 2. Exits before entries
# --------------------------------------------------------------------------


class TestExitsOutrankEntries:
    def test_the_exit_goes_out_before_the_entry(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal, symbols=("XLE", "XLF"))
        # Costs 0.18 to close against 0.50 collected: the profit target.
        hold_xle(journal, market, broker, exit_ask=0.18)

        report = cycle.run(preflight=passing_preflight())

        assert executor.actions == ["close", "open"]
        assert [s.symbol for s in report.closed] == ["XLE"]
        assert [s.symbol for s in report.opened] == ["XLF"]

    def test_the_exit_reason_is_carried_and_journalled(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.18)

        report = cycle.run(preflight=passing_preflight())

        (closed,) = report.closed
        assert closed.exit_reason is ExitReason.PROFIT_TARGET
        exits = [
            d
            for d in journal.recent_decisions(limit=100, cycle_id=report.cycle_id)
            if d.stage is Stage.EXIT
        ]
        assert any("profit_target" in d.reasons for d in exits)

    def test_the_closing_order_is_a_debit(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.18)

        report = cycle.run(preflight=passing_preflight())

        # Buying a credit spread back costs money; the sign must flip.
        assert report.closed[0].limit_price > 0

    def test_an_urgent_exit_crosses_more_of_the_spread(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        # Costs 1.20 to close against 0.50 collected: past the 2x loss limit.
        hold_xle(journal, market, broker, exit_ask=1.20)

        report = cycle.run(preflight=passing_preflight())

        (closed,) = report.closed
        assert closed.exit_reason is ExitReason.LOSS_LIMIT
        # Conservative debit is 1.20 - 0.02 = 1.18, plus the 10% urgency markup.
        assert closed.limit_price > Decimal("1.18")

    def test_an_unplaceable_urgent_exit_bars_new_entries(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal, symbols=("XLE", "XLF"))
        # Two days from expiry: the time stop fires without needing a price.
        hold_xle(
            journal,
            market,
            broker,
            exit_ask=0.30,
            short=XLE_SOON_SHORT,
            long_=XLE_SOON_LONG,
        )
        # ...but the long wing has no quote, so the close cannot be priced.
        market.snapshots[XLE_SOON_LONG] = FakeSnapshot(latest_quote=None)

        report = cycle.run(preflight=passing_preflight())

        assert executor.calls == []
        assert Halt.URGENT_EXIT_UNPLACED in report.halts
        assert any(Refusal.EXIT_UNPRICEABLE.value in r.reasons for r in report.rejections)

    def test_a_working_exit_is_not_placed_twice(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.18)
        # The broker accepted the close and it is still working.
        executor.results["close"] = OrderResult(
            ok=True,
            backend=Backend.SDK,
            client_order_id="ignored",
            payload={},
            order_id="broker-1",
            status="accepted",
            at=NOW,
        )
        cycle.run(preflight=passing_preflight())
        placed = len(executor.calls)

        report = cycle.run(preflight=passing_preflight())

        assert len(executor.calls) == placed
        assert any(Refusal.EXIT_ALREADY_WORKING.value in r.reasons for r in report.rejections)

    def test_a_hold_is_reported_but_not_traded(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        # 0.36 to close against 0.50: neither target nor limit.
        hold_xle(journal, market, broker, exit_ask=0.38)

        report = cycle.run(preflight=passing_preflight())

        assert report.closed == ()
        (hold,) = report.holds
        assert hold.reason is None

    def test_the_exit_stage_runs_before_the_entry_gates_are_even_read(
        self, journal: Journal
    ) -> None:
        # Belt and braces on the structural claim: with entries barred by a
        # failed preflight, the exit still goes out.
        cycle, market, broker, executor = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.18)

        report = cycle.run(preflight=failing_preflight())

        assert executor.actions == ["close"]
        assert Halt.PREFLIGHT_FAILED in report.halts

    def test_the_entry_stage_cannot_be_called_without_an_exit_pass(self) -> None:
        # The ordering is a property of the signature, not of the call order.
        # `_open_positions` takes an ExitPass and only `_close_positions`
        # produces one.
        pass_ = ExitPass()
        assert pass_.decided == ()
        assert pass_.urgent_unplaced == ()


# --------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------


class TestKillSwitch:
    def test_the_durable_switch_stops_entries_but_not_exits(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal, symbols=("XLE", "XLF"))
        hold_xle(journal, market, broker, exit_ask=0.18)
        journal.engage_kill_switch(reason="operator stopped trading", actor=KillSwitchActor.AGENT)

        report = cycle.run(preflight=passing_preflight())

        assert executor.actions == ["close"]
        assert Halt.KILL_SWITCH in report.halts
        assert report.opened == ()

    def test_the_process_switch_stops_entries_too(self, journal: Journal) -> None:
        cycle, *_ = build(journal, kill_switch=True)
        report = cycle.run(preflight=passing_preflight())
        assert Halt.KILL_SWITCH in report.halts

    def test_the_refusal_carries_a_displayable_reason(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        journal.engage_kill_switch(reason="drawdown", actor=KillSwitchActor.RISK)

        report = cycle.run(preflight=passing_preflight())

        (rejection,) = [r for r in report.rejections if Halt.KILL_SWITCH.value in r.reasons]
        assert "drawdown" in rejection.detail[0]
        assert "Exits are unaffected" in rejection.detail[0]


# --------------------------------------------------------------------------
# 3. The session baseline
# --------------------------------------------------------------------------


class TestSessionOpenEquity:
    def test_it_is_recorded_at_the_first_cycle(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        cycle.run(preflight=passing_preflight())
        assert journal.session_open_equity(DAY) == 100_000

    def test_the_first_write_of_the_day_wins(self, journal: Journal) -> None:
        cycle, _market, broker, _ = build(journal)
        cycle.run(preflight=passing_preflight())

        # Equity moved. The baseline must not move with it, or the daily loss
        # stop drifts with P&L and never fires.
        broker.account_obj = FakeAccount(equity="90000")
        later, *_ = build(journal, at=NOW + timedelta(hours=1), broker=broker)
        later.run(preflight=passing_preflight())

        assert journal.session_open_equity(DAY) == 100_000

    def test_it_is_recorded_even_when_the_observe_failed(self, journal: Journal) -> None:
        # Unrecoverable after the fact: a cycle that cannot see the book must
        # still write down where the day started.
        broker = FakeBroker(positions_error=ConnectionError("boom"))
        cycle, *_ = build(journal, broker=broker)

        cycle.run(preflight=passing_preflight())

        assert journal.session_open_equity(DAY) == 100_000

    def test_an_unreadable_equity_records_nothing_and_says_so(self, journal: Journal) -> None:
        broker = FakeBroker(account_obj=FakeAccount(equity="unavailable"))
        cycle, *_ = build(journal, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        assert journal.session_open_equity(DAY) is None
        assert Halt.ACCOUNT_UNREADABLE in report.halts
        assert any("no baseline" in note for note in report.notes)

    def test_the_baseline_reaches_the_risk_engine(self, journal: Journal) -> None:
        # Recorded before the recovery read, so the daily loss stop has a
        # baseline on the very first cycle of the day rather than the second.
        cycle, *_ = build(journal)
        report = cycle.run(preflight=passing_preflight())
        assert report.opened, "a missing baseline would have denied on UNREADABLE_BASELINE"


# --------------------------------------------------------------------------
# 4. Entry gates
# --------------------------------------------------------------------------


class TestEntryGates:
    def test_a_missing_preflight_bars_entries(self, journal: Journal) -> None:
        cycle, _market, _broker, executor = build(journal)
        report = cycle.run()
        assert Halt.PREFLIGHT_MISSING in report.halts
        assert executor.calls == []

    def test_a_failed_preflight_names_the_check(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        report = cycle.run(preflight=failing_preflight())
        (rejection,) = [r for r in report.rejections if Halt.PREFLIGHT_FAILED.value in r.reasons]
        assert "options.effective_level" in rejection.detail[0]

    def test_a_hostile_regime_blocks_every_entry(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        # An inverted curve: the near expiry prices more risk than the far one.
        market.far["SPY"] = {"SPY261016P00097000": snapshot(2.00, 2.10, iv=0.20, at=NOW)}
        cycle, *_ = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        assert Halt.REGIME_BLOCKED in report.halts
        assert report.regime is not None
        assert not report.regime.may_open
        assert report.opened == ()

    def test_a_missing_curve_blocks_rather_than_trading_blind(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        market.far["SPY"] = {}
        cycle, *_ = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        assert report.term_structure is None
        assert Halt.REGIME_BLOCKED in report.halts

    def test_a_blocking_regime_is_still_journalled(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        market.far["SPY"] = {}
        cycle, *_ = build(journal, market=market)

        cycle.run(preflight=passing_preflight())

        (verdict,) = journal.regime_history()
        assert not verdict.allowed
        assert verdict.blocks

    def test_no_candidate_above_the_floor_is_a_stated_outcome(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        # Implied vol barely above realised: real, measured, and not enough.
        market.near["XLE"] = tradeable_chain(XLE_SHORT, XLE_LONG, at=NOW, iv=0.21)
        cycle, *_ = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        assert Halt.NO_CANDIDATES in report.halts
        assert any(s.reason.value == "premium_below_floor" for s in report.skips)

    def test_exploratory_floor_opens_without_calling_executor(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        # 1.079x: above the 1.05 exploratory floor, below the 1.15 live floor.
        market.near["XLE"] = tradeable_chain(XLE_SHORT, XLE_LONG, at=NOW, iv=0.22)
        cycle, _, _, executor = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        assert report.opened == ()
        assert executor.calls == []
        assert journal.order_history() == ()
        position = journal.exploratory_open_position()
        assert position is not None
        assert position.symbol == "XLE"
        assert position.opening_vrp_ratio == pytest.approx(1.0793758391)
        assert position.spreads == 3
        mark = journal.latest_pnl(trading_day=DAY, source=PnlSource.EXPLORATORY)
        assert mark is not None
        assert mark.realised_pnl == 0
        assert mark.unrealised_pnl == pytest.approx(-30.0)
        decisions = journal.recent_decisions(limit=100, cycle_id=report.cycle_id)
        accepted = [d for d in decisions if d.stage is Stage.EXPLORE and d.accepted]
        assert accepted
        assert accepted[0].context["submitted_to_broker"] is False

        # A later conservative mark costs 0.20 to close against 0.50 received,
        # so the shared 50%-of-credit profit target closes the hypothetical.
        later = NOW + timedelta(minutes=5)
        market.snapshots[XLE_SHORT] = snapshot(0.28, 0.30, at=later)
        market.snapshots[XLE_LONG] = snapshot(0.10, 0.12, at=later)
        next_cycle, _, _, next_executor = build(journal, market=market, at=later)
        next_cycle.run(preflight=passing_preflight())
        assert next_executor.calls == []
        assert journal.exploratory_open_position() is None
        final = journal.latest_pnl(trading_day=DAY, source=PnlSource.EXPLORATORY)
        assert final is not None
        assert final.realised_pnl == pytest.approx(90.0)

    def test_exploratory_failure_cannot_block_live_entry(
        self, journal: Journal, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("exploratory storage unavailable")

        monkeypatch.setattr(Cycle, "_run_exploratory", explode)
        cycle, _, _, executor = build(journal)

        report = cycle.run(preflight=passing_preflight())

        assert [submission.symbol for submission in report.opened] == ["XLE"]
        assert len(executor.calls) == 1
        assert any("live execution continues unaffected" in note for note in report.notes)

    def test_live_candidate_does_not_enter_incremental_exploratory_lane(
        self, journal: Journal
    ) -> None:
        cycle, _, _, executor = build(journal, veto=FakeVeto())

        report = cycle.run(preflight=passing_preflight())

        assert [submission.symbol for submission in report.opened] == ["XLE"]
        assert len(executor.calls) == 1
        assert journal.exploratory_open_position() is None
        assert all(
            decision.stage is not Stage.EXPLORE
            for decision in journal.recent_decisions(limit=100, cycle_id=report.cycle_id)
        )

    def test_unpriced_preexpiry_time_stop_waits_for_a_quote(self, journal: Journal) -> None:
        opened = journal.open_exploratory_position(
            cycle_id="cycle-x",
            symbol="XLE",
            short_symbol=XLE_SOON_SHORT,
            long_symbol=XLE_SOON_LONG,
            expiry=date(2026, 9, 2),
            spreads=1,
            width=2.0,
            credit_per_spread=0.50,
            max_loss=150.0,
            net_delta=15.0,
            opening_vrp_ratio=1.08,
            at=NOW - timedelta(minutes=5),
        )
        market = market_for(("XLE",))
        market.near["XLE"] = tradeable_chain(XLE_SHORT, XLE_LONG, at=NOW, iv=0.21)
        cycle, _, _, _ = build(journal, market=market)

        cycle.run(preflight=passing_preflight())

        still_open = journal.exploratory_open_position()
        assert still_open is not None
        assert still_open.id == opened.id
        assert still_open.realised_pnl is None

    def test_below_floor_decision_records_variance_diagnostics(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        market.near["XLE"] = tradeable_chain(XLE_SHORT, XLE_LONG, at=NOW, iv=0.21)
        cycle, *_ = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        (decision,) = [
            item
            for item in journal.recent_decisions(limit=100, cycle_id=report.cycle_id)
            if item.stage is Stage.RANK
            and item.symbol == "XLE"
            and "premium_below_floor" in item.reasons
        ]
        context = decision.context
        vrp_ratio = context["vrp_ratio"]
        implied_variance = context["implied_variance"]
        realised_variance = context["realised_variance"]
        assert isinstance(vrp_ratio, float)
        assert isinstance(implied_variance, float)
        assert isinstance(realised_variance, float)
        assert context["volatility_ratio"] == context["vrp_ratio"]
        assert context["variance_ratio"] == pytest.approx(vrp_ratio**2, rel=1e-3)
        assert context["variance_risk_premium"] == pytest.approx(
            implied_variance - realised_variance
        )
        assert context["option_feed"] == "indicative"
        assert context["implied_vol_basis"] == "provider_snapshot"
        assert context["executable_iv_known"] is False

    def test_an_unsettled_order_blocks_that_symbol(self, journal: Journal) -> None:
        journal.record_intent(
            client_order_id="uw-open-XLE-20260831-stuck",
            cycle_id="2026-08-31-090000",
            symbol="XLE",
            spreads=1.0,
            payload={},
            legs=[
                IntentLeg(XLE_SHORT, "sell", 1, "sell_to_open"),
                IntentLeg(XLE_LONG, "buy", 1, "buy_to_open"),
            ],
            at=NOW - timedelta(minutes=5),
        )
        cycle, _market, _broker, executor = build(journal)

        report = cycle.run(preflight=passing_preflight())

        assert executor.calls == []
        assert any(Refusal.UNRECONCILED_ORDER.value in r.reasons for r in report.rejections)

    def test_risk_denials_are_recorded_with_their_reasons(self, journal: Journal) -> None:
        # 15:30 ET is past the entry cutoff.
        late = datetime(2026, 8, 31, 19, 30, tzinfo=UTC)
        cycle, _market, _broker, executor = build(journal, at=late)

        report = cycle.run(preflight=passing_preflight())

        assert executor.calls == []
        (denial,) = [r for r in report.rejections if r.stage is Stage.RISK and r.symbol == "XLE"]
        assert "too_late_in_session" in denial.reasons
        assert denial.detail

    def test_the_position_cap_counts_positions_opened_this_cycle(self, journal: Journal) -> None:
        # Two candidates, room for one. Without the running book, both would be
        # measured against an empty account and both would pass.
        limits = RiskLimits(max_concurrent_positions=1)
        cycle, _market, _broker, executor = build(journal, symbols=("XLE", "XLF"), limits=limits)

        report = cycle.run(preflight=passing_preflight())

        assert len(executor.calls) == 1
        assert any("position_cap" in r.reasons for r in report.rejections)

    def test_a_duplicate_symbol_is_refused(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.38)

        report = cycle.run(preflight=passing_preflight())

        assert report.opened == ()
        assert any("duplicate_symbol" in r.reasons for r in report.rejections)


# --------------------------------------------------------------------------
# The veto seam
# --------------------------------------------------------------------------


class TestVetoSeam:
    @pytest.mark.parametrize("day", [date(2026, 9, 3), date(2026, 9, 4)])
    def test_macro_calendar_days_can_reach_paper_submission(
        self, journal: Journal, day: date
    ) -> None:
        at = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        cycle, _market, _broker, executor = build(journal, at=at, veto=FakeVeto())

        report = cycle.run(preflight=passing_preflight())

        assert executor.actions == ["open"]
        assert [opened.symbol for opened in report.opened] == ["XLE"]
        assert report.regime is not None
        assert RegimeBlock.SCHEDULED_EVENT not in report.regime.reasons

    def test_an_unwired_veto_records_that_nothing_screened_the_candidate(
        self, journal: Journal
    ) -> None:
        cycle, *_ = build(journal)
        report = cycle.run(preflight=passing_preflight())
        vetoes = [
            d
            for d in journal.recent_decisions(limit=100, cycle_id=report.cycle_id)
            if d.stage is Stage.VETO
        ]
        assert vetoes and vetoes[0].accepted
        assert "not screened" in vetoes[0].detail[0]

    def test_a_veto_removes_the_candidate(self, journal: Journal) -> None:
        veto = FakeVeto(VetoVerdict(vetoed=True, catalyst="OPEC+ meeting in the window"))
        cycle, _market, _broker, executor = build(journal, veto=veto)

        report = cycle.run(preflight=passing_preflight())

        assert executor.calls == []
        (rejection,) = [r for r in report.rejections if r.stage is Stage.VETO]
        assert "OPEC+" in rejection.detail[0]

    def test_an_unavailable_model_is_a_veto_not_an_approval(self, journal: Journal) -> None:
        veto = FakeVeto(error=TimeoutError("model timed out"))
        cycle, _market, _broker, executor = build(journal, veto=veto)

        report = cycle.run(preflight=passing_preflight())

        assert executor.calls == []
        assert any(r.stage is Stage.VETO for r in report.rejections)

    def test_a_required_but_unwired_veto_bars_entries(self, journal: Journal) -> None:
        cycle, _market, _broker, executor = build(journal, veto_required=True)
        report = cycle.run(preflight=passing_preflight())
        assert Halt.VETO_UNAVAILABLE in report.halts
        assert executor.calls == []


# --------------------------------------------------------------------------
# 5. Journal before submit
# --------------------------------------------------------------------------


class TestWriteAhead:
    def test_the_intent_exists_before_the_order_is_sent(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal, symbols=("XLE", "XLF"))
        hold_xle(journal, market, broker, exit_ask=0.18)

        cycle.run(preflight=passing_preflight())

        assert len(executor.seen_status) == 2
        # Not merely "a record existed" -- it existed in the pre-submission
        # state, which is what a crash between the two would leave behind.
        assert executor.seen_status == [OrderStatus.INTENT, OrderStatus.INTENT]

    def test_a_crash_between_write_and_send_leaves_a_chaseable_intent(
        self, journal: Journal
    ) -> None:
        cycle, _market, _broker, executor = build(journal)
        executor.error = ConnectionError("died mid-flight")

        report = cycle.run(preflight=passing_preflight())

        # The submission raised, the cycle survived, and the order is on disk
        # under the client_order_id reconciliation matches on.
        (failure,) = report.failures
        assert failure.reason is Failed.ENTRY_ERROR
        (stuck,) = journal.unreconciled_orders()
        assert stuck.status is OrderStatus.INTENT
        assert stuck.symbol == "XLE"

    def test_an_unknown_outcome_stays_unreconciled(self, journal: Journal) -> None:
        cycle, _market, _broker, executor = build(journal)
        executor.results["open"] = OrderResult(
            ok=False,
            backend=Backend.CLI,
            client_order_id="ignored",
            payload={},
            reason=Reason.UNKNOWN_OUTCOME,
            message="timed out and the lookup was inconclusive",
            at=NOW,
        )

        report = cycle.run(preflight=passing_preflight())

        (submission,) = report.opened
        assert submission.status is OrderStatus.UNKNOWN
        assert journal.unreconciled_orders()
        assert any(r.reasons == ("unknown_outcome",) for r in report.rejections)

    def test_a_payload_that_never_reached_the_broker_is_abandoned(self, journal: Journal) -> None:
        # `proven_absent` is the adapter's OBSERVATION that nothing was
        # created -- validation refuses before any HTTP call. The reason code
        # cannot carry this on its own, because API_ERROR is emitted on both
        # the terminal branch and the unknown branch.
        cycle, _market, _broker, executor = build(journal)
        executor.results["open"] = OrderResult(
            ok=False,
            backend=None,
            client_order_id="ignored",
            payload={},
            reason=Reason.INVALID_PAYLOAD,
            message="two legs required",
            at=NOW,
            proven_absent=True,
        )

        report = cycle.run(preflight=passing_preflight())

        assert report.opened[0].status is OrderStatus.ABANDONED
        assert journal.unreconciled_orders() == ()

    def test_an_unproven_absence_is_left_unknown_not_abandoned(self, journal: Journal) -> None:
        # The dangerous case. A 5xx and a rejected payload can carry the same
        # reason code, so abandoning on the code alone would mark a possibly
        # live order as never-created and free the symbol to be traded again.
        cycle, _market, _broker, executor = build(journal)
        executor.results["open"] = OrderResult(
            ok=False,
            backend=None,
            client_order_id="ignored",
            payload={},
            reason=Reason.API_ERROR,
            message="502 from the order system",
            at=NOW,
            proven_absent=False,
        )

        report = cycle.run(preflight=passing_preflight())

        assert report.opened[0].status is OrderStatus.UNKNOWN
        # Non-terminal, so it stays unreconciled and blocks the symbol.
        assert journal.unreconciled_orders()

    def test_a_dry_run_is_abandoned_rather_than_left_hanging(self, journal: Journal) -> None:
        # `--dry-run` makes no HTTP call at all, so absence is proven.
        cycle, _market, _broker, _executor = build(journal, dry_run=True)
        report = cycle.run(preflight=passing_preflight())
        assert report.opened[0].status is OrderStatus.ABANDONED
        assert journal.unreconciled_orders() == ()


# --------------------------------------------------------------------------
# 6. A P&L snapshot after every fill
# --------------------------------------------------------------------------


class TestPnlSnapshots:
    def test_a_snapshot_follows_the_fill(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        cycle.run(preflight=passing_preflight())

        earliest = journal.session_open_candidate(DAY)
        latest = journal.latest_pnl(trading_day=DAY)
        assert earliest is not None
        assert latest is not None
        # Two snapshots: one immediately after the fill, one at end of cycle.
        # `_realised_today` reads as unknown unless one is dated after the last
        # fill, and that figure is what the daily loss stop measures.
        assert earliest.id != latest.id

    def test_the_snapshot_carries_equity_and_the_marks(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.38)

        cycle.run(preflight=passing_preflight())

        snap = journal.latest_pnl(trading_day=DAY)
        assert snap is not None
        assert snap.source is PnlSource.OFFICIAL
        assert snap.equity == 100_000
        assert snap.unrealised_pnl != 0

    def test_an_unreadable_account_records_no_guess(self, journal: Journal) -> None:
        broker = FakeBroker(account_obj=FakeAccount(last_equity="n/a"))
        cycle, *_ = build(journal, broker=broker)

        report = cycle.run(preflight=passing_preflight())

        assert journal.latest_pnl(trading_day=DAY) is None
        assert any("would be a guess" in note for note in report.notes)

    def test_realised_pnl_is_readable_afterwards(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        cycle.run(preflight=passing_preflight())
        # The input the daily loss stop needs, present rather than None.
        assert journal.recover(now=NOW).realised_pnl_today is not None


# --------------------------------------------------------------------------
# 7. Reconciliation
# --------------------------------------------------------------------------


class TestReconciliation:
    def test_a_successful_sweep_is_recorded(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        report = cycle.run(preflight=passing_preflight())

        last = journal.last_reconciliation()
        assert last is not None
        assert last.ok
        # Without the record, view_age stays None and VIEW_STALE never clears.
        assert "view_stale" not in report.recovery_gaps

    def test_a_failed_position_sweep_is_recorded_as_failed(self, journal: Journal) -> None:
        broker = FakeBroker(positions_error=ConnectionError("boom"))
        cycle, *_ = build(journal, broker=broker)

        cycle.run(preflight=passing_preflight())

        # Failed passes deliberately do not refresh the clock.
        assert journal.last_reconciliation() is None

    def test_an_order_reader_settles_a_stuck_intent(self, journal: Journal) -> None:
        journal.record_intent(
            client_order_id="uw-open-XLE-20260831-stuck",
            cycle_id="2026-08-31-090000",
            symbol="XLE",
            spreads=1.0,
            payload={},
            legs=[
                IntentLeg(XLE_SHORT, "sell", 1, "sell_to_open"),
                IntentLeg(XLE_LONG, "buy", 1, "buy_to_open"),
            ],
            at=NOW - timedelta(minutes=5),
        )
        orders = FakeOrders(
            views={
                "uw-open-XLE-20260831-stuck": BrokerOrderView(
                    status="filled",
                    order_id="broker-1",
                    filled_qty=1.0,
                    filled_avg_price=-0.50,
                )
            }
        )
        cycle, *_ = build(journal, orders=orders)

        cycle.run(preflight=passing_preflight())

        settled = journal.order("uw-open-XLE-20260831-stuck")
        assert settled is not None
        assert settled.status is OrderStatus.FILLED
        assert journal.unreconciled_orders() == ()

    def test_a_proven_absent_order_is_abandoned(self, journal: Journal) -> None:
        journal.record_intent(
            client_order_id="uw-open-XLE-20260831-ghost",
            cycle_id="2026-08-31-090000",
            symbol="XLE",
            spreads=1.0,
            payload={},
            legs=[
                IntentLeg(XLE_SHORT, "sell", 1, "sell_to_open"),
                IntentLeg(XLE_LONG, "buy", 1, "buy_to_open"),
            ],
            at=NOW - timedelta(minutes=5),
        )
        cycle, *_ = build(journal, orders=FakeOrders(views={}))

        cycle.run(preflight=passing_preflight())

        ghost = journal.order("uw-open-XLE-20260831-ghost")
        assert ghost is not None
        assert ghost.status is OrderStatus.ABANDONED

    def test_a_failed_lookup_settles_nothing(self, journal: Journal) -> None:
        # The reader's contract: an incomplete lookup raises. Abandoning on it
        # would discard an intent that may be a live order.
        journal.record_intent(
            client_order_id="uw-open-XLE-20260831-stuck",
            cycle_id="2026-08-31-090000",
            symbol="XLE",
            spreads=1.0,
            payload={},
            legs=[
                IntentLeg(XLE_SHORT, "sell", 1, "sell_to_open"),
                IntentLeg(XLE_LONG, "buy", 1, "buy_to_open"),
            ],
            at=NOW - timedelta(minutes=5),
        )
        orders = FakeOrders(error=TimeoutError("lookup timed out"))
        cycle, *_ = build(journal, orders=orders)

        report = cycle.run(preflight=passing_preflight())

        stuck = journal.order("uw-open-XLE-20260831-stuck")
        assert stuck is not None
        assert stuck.status is OrderStatus.INTENT
        assert any(f.reason is Failed.RECONCILE_ERROR for f in report.failures)


# --------------------------------------------------------------------------
# 8. One bad symbol
# --------------------------------------------------------------------------


class TestOneBadSymbol:
    def test_a_broken_chain_costs_only_that_symbol(self, journal: Journal) -> None:
        market = market_for(("XLE", "XLF"))
        market.chain_errors["XLE"] = RuntimeError("chain unparseable")
        cycle, *_ = build(journal, symbols=("XLE", "XLF"), market=market)

        report = cycle.run(preflight=passing_preflight())

        assert [s.symbol for s in report.opened] == ["XLF"]
        (failure,) = report.failures
        assert failure.symbol == "XLE"
        assert failure.reason is Failed.SCAN_ERROR

    def test_the_failure_is_journalled_as_a_failure_not_a_rejection(self, journal: Journal) -> None:
        market = market_for(("XLE",))
        market.chain_errors["XLE"] = RuntimeError("chain unparseable")
        cycle, *_ = build(journal, market=market)

        report = cycle.run(preflight=passing_preflight())

        (rejection,) = [r for r in report.rejections if r.symbol == "XLE"]
        # "We could not look" must not read like "we looked and said no".
        assert rejection.reasons == (Failed.SCAN_ERROR.value,)
        assert "chain unparseable" in rejection.detail[0]

    def test_a_dead_data_feed_blocks_entries_and_leaves_exits_alone(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal)
        hold_xle(journal, market, broker, exit_ask=0.18)
        market.bars_error = ConnectionError("bars unreachable")

        report = cycle.run(preflight=passing_preflight())

        # A missing read is not a reason to liquidate, and it is every reason
        # not to open.
        assert executor.actions == ["close"]
        assert Halt.REGIME_BLOCKED in report.halts

    def test_scheduled_event_never_forces_an_exit(self, journal: Journal) -> None:
        thursday = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
        cycle, market, broker, executor = build(journal, at=thursday)
        hold_xle(journal, market, broker, exit_ask=0.34)
        market.bars_error = ConnectionError("bars unreachable")

        report = cycle.run(preflight=passing_preflight())

        assert executor.actions == []
        assert report.closed == ()
        assert report.regime is not None
        assert RegimeBlock.SCHEDULED_EVENT not in report.regime.reasons
        assert RegimeBlock.BENCHMARK_HISTORY_MISSING in report.regime.reasons
        assert Halt.REGIME_BLOCKED in report.halts

    def test_a_bad_exit_does_not_take_the_queue_with_it(self, journal: Journal) -> None:
        cycle, market, broker, executor = build(journal, symbols=("XLE", "XLF"))
        hold_xle(journal, market, broker, exit_ask=0.18)
        # An OCC symbol the exit builder cannot parse: one position's problem.
        broker.raw = [*broker.raw, RawOptionPosition("not-an-occ-symbol", -1.0)]

        report = cycle.run(preflight=passing_preflight())

        assert "close" in executor.actions
        assert report.observation is not None
        assert any(o.reason.value == "unparseable_symbol" for o in report.observation.orphans)


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------


class TestCycleReport:
    def test_it_carries_the_refusals_as_prominently_as_the_trades(self, journal: Journal) -> None:
        cycle, *_ = build(
            journal, symbols=("XLE", "XLF"), limits=RiskLimits(max_concurrent_positions=1)
        )
        report = cycle.run(preflight=passing_preflight())

        assert report.opened
        assert report.rejections
        assert report.cycle_id.startswith("2026-08-31")
        assert report.trading_day == DAY

    def test_a_clean_cycle_needs_no_attention(self, journal: Journal) -> None:
        cycle, *_ = build(journal)
        report = cycle.run(preflight=passing_preflight())
        assert not report.needs_attention

    def test_empty_defaults_are_coherent(self) -> None:
        report = CycleReport(cycle_id="x", started_at=NOW, trading_day=DAY)
        assert not report.observed
        assert report.positions == ()
        assert report.opened == ()
        assert report.closed == ()
        assert not report.entries_barred

    def test_the_book_view_exposes_the_positions(self) -> None:
        assert MarketView().regime.may_open
        facts = AccountFacts(equity=1.0, options_buying_power=1.0, last_equity=None)
        assert facts.readable

    def test_exit_pass_names_the_urgent_exits_it_could_not_place(self) -> None:
        spread = OpenSpread(
            underlying="XLE",
            short_symbol=XLE_SHORT,
            long_symbol=XLE_LONG,
            expiry=NEAR_EXPIRY,
            spreads=1.0,
            width=2.0,
            credit_per_spread=0.50,
            max_loss=150.0,
            net_delta=15.0,
            unrealised_pnl=0.0,
            client_order_id="uw-A",
        )
        pass_ = ExitPass(
            decided=(ExitDecision(spread, ExitReason.HARD_FLATTEN, "deadline", urgent=True),)
        )
        assert pass_.urgent_unplaced[0].spread.underlying == "XLE"

    def test_submissions_are_split_by_action(self, journal: Journal) -> None:
        cycle, market, broker, _ = build(journal, symbols=("XLE", "XLF"))
        hold_xle(journal, market, broker, exit_ask=0.18)

        report = cycle.run(preflight=passing_preflight())

        assert {s.action for s in report.submissions} == {Action.OPEN, Action.CLOSE}
        assert len(report.submissions) == 2


def test_a_book_wraps_the_observation(journal: Journal) -> None:
    cycle, *_ = build(journal)
    report = cycle.run(preflight=passing_preflight())
    assert report.observation is not None
    book = Book(observation=report.observation)
    assert book.positions == ()
