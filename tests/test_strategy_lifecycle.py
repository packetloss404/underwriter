"""Production wiring exercised with a real adapter and isolated broker state.

These are deterministic integration tests. They do not contact Alpaca or a
model, do not run the supervisor, and make no claim about real paper fills.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.trading.client import TradingClient
from freezegun import freeze_time

from tests.test_cycle import (
    DAY,
    NOW,
    XLE_LONG,
    XLE_SHORT,
    FakeAccount,
    FakeBroker,
    FakeMarket,
    FakeVeto,
    hold_xle,
    market_for,
    snapshot,
    tradeable_chain,
)
from tests.test_cycle import (
    FakeClock as CycleClock,
)
from tests.test_preflight import FakeClock
from underwriter import live, preflight
from underwriter.config import Settings
from underwriter.cycle import Halt
from underwriter.endofday import DailyCloseJob, read_status
from underwriter.execution import (
    Backend,
    BackendOutcome,
    BrokerOrder,
    ExecutionAdapter,
    Kind,
    LegFill,
    MultiLegOrder,
    Side,
)
from underwriter.journal import FillSource, Journal, KillSwitchActor, OrderStatus, PnlSource
from underwriter.live import Agent
from underwriter.positions import RawOptionPosition


@dataclass(slots=True)
class SessionBroker(FakeBroker):
    is_open: bool = True

    def clock(self) -> FakeClock:
        return FakeClock(is_open=self.is_open)

    def is_trading_day(self, day: date) -> bool:
        return day == DAY


@dataclass(slots=True)
class FillingBackend:
    broker: SessionBroker
    name: Backend = Backend.SDK
    journal: Journal | None = None
    calls: list[MultiLegOrder] = field(default_factory=list)
    orders: dict[str, BrokerOrder] = field(default_factory=dict)

    def unavailable_reason(self) -> str | None:
        return None

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> BackendOutcome:
        assert not dry_run
        assert self.journal is not None
        intent = self.journal.order(order.client_order_id)
        assert intent is not None and intent.status is OrderStatus.INTENT
        self.calls.append(order)
        if order.is_credit:
            self.broker.raw = [
                RawOptionPosition(
                    leg.symbol,
                    float(order.qty * leg.ratio_qty) * (-1 if leg.side is Side.SELL else 1),
                )
                for leg in order.legs
            ]
        else:
            self.broker.raw = []
            self.broker.account_obj = FakeAccount(equity="100078")
        found = BrokerOrder(
            f"isolated-{len(self.calls)}",
            order.client_order_id,
            "filled",
            Decimal(order.qty),
            order.limit_price,
        )
        self.orders[order.client_order_id] = found
        return BackendOutcome(Kind.ACCEPTED, order=found)

    def lookup(self, client_order_id: str) -> BackendOutcome:
        found = self.orders.get(client_order_id)
        return BackendOutcome(Kind.ABSENT if found is None else Kind.ACCEPTED, order=found)

    def fetch_legs(self, order_id: str) -> tuple[LegFill, ...] | None:
        return None


def wired_agent(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    *,
    at: datetime = NOW,
    broker: SessionBroker | None = None,
    backend: FillingBackend | None = None,
    model_configured: bool = True,
) -> tuple[Agent, FakeMarket, SessionBroker, FillingBackend]:
    broker = broker or SessionBroker()
    backend = backend or FillingBackend(broker)
    market = market_for(("XLE",), at=at)
    if at.date() != DAY:
        near = (at + timedelta(days=10)).strftime("%y%m%d")
        far = (at + timedelta(days=45)).strftime("%y%m%d")
        market.near["XLE"] = tradeable_chain(
            f"XLE{near}P00097000", f"XLE{near}P00095000", at=at
        )
        market.near["SPY"] = {f"SPY{near}P00097000": snapshot(1, 1.06, iv=0.3, at=at)}
        market.far["SPY"] = {f"SPY{far}P00097000": snapshot(2, 2.1, iv=0.35, at=at)}
    adapter = ExecutionAdapter(primary=backend)
    monkeypatch.setattr(live, "paper_broker", lambda *_args: broker)
    monkeypatch.setattr(live, "MarketData", lambda *_args: None)
    monkeypatch.setattr(live, "LiveMarket", lambda _data: market)
    def adapter_for(*, sdk_client: TradingClient) -> ExecutionAdapter:
        assert isinstance(sdk_client, TradingClient)
        return adapter

    monkeypatch.setattr(live, "build_adapter", adapter_for)
    monkeypatch.setattr(live, "build_veto", lambda **_kwargs: FakeVeto())
    monkeypatch.setattr(
        preflight,
        "check_cli",
        lambda _binary: [preflight.Check("cli.available", preflight.Status.OK, "isolated CLI")],
    )
    # Settings must never pick up a developer's real provider key.
    for variable in (
        "ALPACA_LIVE_TRADE",
        "UNDERWRITER_KILL_SWITCH",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "MINIMAX_API_KEY",
        "FEATHERLESS_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(
        _env_file=None,
        ALPACA_API_KEY="isolated-key",
        ALPACA_SECRET_KEY="isolated-secret",
        OPENAI_API_KEY="isolated-model-key" if model_configured else None,
        UNDERWRITER_MODEL_PROVIDER="auto",
    )
    agent = live.build_agent(settings, str(database), clock=lambda: at)
    backend.journal = agent.journal
    return agent, market, broker, backend


def held_quotes(market: FakeMarket, at: datetime, *, ask: float) -> None:
    market.snapshots = {
        XLE_SHORT: snapshot(ask - 0.04, ask, delta=-0.20, at=at),
        XLE_LONG: snapshot(0.02, 0.06, delta=-0.08, at=at),
    }


def test_production_missing_model_blocks_entries_but_observes_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with freeze_time(NOW):
        agent, market, _, backend = wired_agent(
            monkeypatch, tmp_path / "journal.db", model_configured=False
        )
        try:
            report = agent()
            assert Halt.VETO_UNAVAILABLE in report.halts
            assert report.observed
            assert report.rankings
            assert market.chain_calls
            assert backend.calls == []
            assert agent.journal.last_reconciliation() is not None
            assert agent.journal.latest_pnl(trading_day=DAY) is not None
        finally:
            agent.close()


@pytest.mark.parametrize(
    "at",
    [
        datetime.fromisoformat("2026-09-07T14:30:00+00:00"),  # Weekday market holiday.
        datetime.fromisoformat("2026-11-27T18:30:00+00:00"),  # After a 13:00 ET close.
    ],
)
def test_broker_closed_on_weekday_blocks_even_fresh_tradeable_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, at: datetime
) -> None:
    from underwriter.runtime import Schedule

    assert Schedule().should_run(at)
    with freeze_time(at):
        agent, _, broker, backend = wired_agent(
            monkeypatch, tmp_path / "journal.db", at=at, broker=SessionBroker(is_open=False)
        )
        try:
            report = agent()
            assert Halt.PREFLIGHT_FAILED in report.halts
            assert report.observed
            assert backend.calls == []
            assert agent.journal.last_reconciliation() is not None
            # The same fresh inputs can open once the authoritative broker
            # clock permits entries, so the closed-market assertion is causal.
            broker.is_open = True
            assert agent().opened
            assert len(backend.calls) == 1
        finally:
            agent.close()


def test_entry_restart_monitor_kill_switch_exit_and_durable_close_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "journal.db"
    with freeze_time(NOW):
        agent, _, broker, backend = wired_agent(monkeypatch, database)
        first = agent()
        assert not first.failures
        assert first.halts == ()
        (opened,) = first.opened
        assert opened.spreads == 3 and opened.limit_price == Decimal("-0.50")
        assert len(broker.raw) == 2
        opened_record = agent.journal.order(opened.client_order_id)
        assert opened_record is not None and opened_record.status is OrderStatus.FILLED
        agent.close()

    # A genuinely fresh writer connection reconstructs the opened spread from
    # the recorded intent and fill. It manages the book instead of duplicating it.
    monitor_at = NOW + timedelta(minutes=5)
    with freeze_time(monitor_at):
        agent, market, _, _ = wired_agent(
            monkeypatch, database, at=monitor_at, broker=broker, backend=backend
        )
        held_quotes(market, monitor_at, ask=0.45)
        monitored = agent()
        assert len(monitored.positions) == 1
        assert len(monitored.holds) == 1
        assert monitored.submissions == ()
        assert len(backend.calls) == 1
        agent.journal.engage_kill_switch(actor=KillSwitchActor.OPERATOR, reason="integration stop")
        agent.close()

    exit_at = NOW + timedelta(minutes=10)
    with freeze_time(exit_at):
        agent, market, _, _ = wired_agent(
            monkeypatch, database, at=exit_at, broker=broker, backend=backend
        )
        held_quotes(market, exit_at, ask=0.20)
        exited = agent()
        assert Halt.KILL_SWITCH in exited.halts
        (closed,) = exited.closed
        assert closed.status is OrderStatus.FILLED
        assert closed.limit_price > 0
        assert broker.raw == []
        assert len(backend.calls) == 2
        agent.close()

    close_at = NOW.replace(hour=20, minute=20)
    with freeze_time(close_at):
        broker.is_open = False
        agent, market, _, _ = wired_agent(
            monkeypatch, database, at=close_at, broker=broker, backend=backend
        )
        directory = tmp_path / "daily-reports"
        try:
            job = DailyCloseJob(directory, agent.end_of_day, clock=lambda: close_at)
            assert job.tick(close_at)
            # Parent snapshots cannot stand in for an execution feed. The
            # first close cannot publish a falsely complete zero shadow.
            assert read_status(directory, close_at)["status"] == "failed"
            assert agent.journal.latest_pnl(trading_day=DAY, source=PnlSource.SHADOW) is None
            assert not (directory / DAY.isoformat() / "history.json").exists()
            assert len(backend.calls) == 2
            assert market.chain_calls == []

            # Explicitly deliver fixture execution events with their own IDs;
            # the real production activity/stream ingestor is not wired.
            for index, order in enumerate(backend.calls):
                filled_at = NOW if index == 0 else exit_at
                agent.journal.record_spread_fill(
                    fill_id=f"isolated-execution-{index}",
                    client_order_id=order.client_order_id,
                    symbol="XLE",
                    spreads=float(order.qty),
                    net_price_per_spread=float(order.limit_price),
                    occurred_at=filled_at,
                    source=FillSource.REST,
                    at=close_at,
                )
            retry = close_at + timedelta(minutes=15)
            refreshed = replace(agent, cycle=replace(agent.cycle, clock=CycleClock(retry)))
            restarted = DailyCloseJob(directory, refreshed.end_of_day, clock=lambda: retry)
            assert restarted.tick(retry)
            assert not DailyCloseJob(directory, agent.end_of_day).tick(retry)
            assert read_status(directory, retry)["status"] == "complete"
            assert len(backend.calls) == 2
            history = json.loads((directory / DAY.isoformat() / "history.json").read_text())
            assert history["totals"]["orders"] == 2
            assert history["totals"]["spread_fills"] == 2
            official = agent.journal.latest_pnl(trading_day=DAY, source=PnlSource.OFFICIAL)
            shadow = agent.journal.latest_pnl(trading_day=DAY, source=PnlSource.SHADOW)
            assert official is not None and official.at == retry
            assert shadow is not None and shadow.at == retry
            assert agent.journal.session_open_equity(DAY) == 100000
        finally:
            agent.close()


def test_missing_model_still_closes_existing_positions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with freeze_time(NOW):
        agent, market, broker, backend = wired_agent(
            monkeypatch, tmp_path / "journal.db", model_configured=False
        )
        try:
            hold_xle(agent.journal, market, broker, exit_ask=0.20)
            report = agent()
            assert Halt.VETO_UNAVAILABLE in report.halts
            assert report.opened == ()
            assert len(report.closed) == 1
            assert report.closed[0].status is OrderStatus.FILLED
            assert len(backend.calls) == 1
            assert not backend.calls[0].is_credit
            assert broker.raw == []
        finally:
            agent.close()
