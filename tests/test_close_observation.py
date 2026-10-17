"""Closing reports observe and reconcile, without trading or inventing a baseline."""

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from tests.test_cycle import DAY, NOW, build, hold_xle
from underwriter.account import AlpacaBroker, bound_read_timeout
from underwriter.config import Settings
from underwriter.dashboard import DashboardConfig, create_app
from underwriter.endofday import DailyCloseJob
from underwriter.execution import (
    Backend,
    BackendOutcome,
    BrokerOrder,
    ExecutionAdapter,
    Kind,
    LegFill,
    MultiLegOrder,
)
from underwriter.journal import Journal, PnlSource
from underwriter.live import Agent, LiveOrderReader

CLOSE = NOW.replace(hour=20, minute=20)


def calendar_broker(*, trading_day: bool = True, is_open: bool = False) -> AlpacaBroker:
    return AlpacaBroker(
        SimpleNamespace(
            get_calendar=lambda _filters: [SimpleNamespace(date=DAY)] if trading_day else [],
            get_clock=lambda: SimpleNamespace(is_open=is_open),
        )
    )


def test_close_observation_marks_the_book_without_scanning_or_trading() -> None:
    with Journal() as journal:
        cycle, market, _, execution = build(journal, at=CLOSE)
        report = cycle.observe_after_close()
        assert report.observed
        assert not report.failures
        assert execution.calls == []
        assert market.chain_calls == []
        assert journal.recent_decisions() == ()
        assert journal.session_open_equity(DAY) is None
        assert journal.last_reconciliation() is not None
        official = journal.latest_pnl(trading_day=DAY)
        shadow = journal.latest_pnl(trading_day=DAY, source=PnlSource.SHADOW)
        assert official is not None and official.at == CLOSE
        assert shadow is not None and shadow.at == CLOSE


def test_close_observation_will_not_place_an_urgent_exit() -> None:
    with Journal() as journal:
        cycle, market, broker, execution = build(journal, at=CLOSE)
        hold_xle(journal, market, broker, exit_ask=1.50)
        report = cycle.observe_after_close()
        assert len(report.positions) == 1
        assert execution.calls == []
        assert report.submissions == ()
        assert market.chain_calls == []


def test_unpriced_held_book_does_not_invent_zero_unrealised_pnl() -> None:
    with Journal() as journal:
        cycle, market, broker, _ = build(journal, at=CLOSE)
        hold_xle(journal, market, broker, exit_ask=1.50)
        market.snapshots.clear()
        cycle.observe_after_close()
        assert journal.latest_pnl(trading_day=DAY) is None


def test_successful_close_callback_returns_exact_session_artifacts() -> None:
    with Journal() as journal:
        cycle, _, _, _ = build(journal, at=CLOSE)
        agent = Agent(
            cycle,
            journal,
            Settings(_env_file=None, ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret"),
            calendar_broker(),
        )
        result = agent.end_of_day(DAY, CLOSE)
        history: Any = result["history"]
        reports: Any = result["reports"]
        assert history["range"]["start"] == history["range"]["end"] == DAY.isoformat()
        assert history["totals"]["orders"] == 0
        assert reports["totals"]["pnl_snapshots"] == 2
        assert journal.session_open_equity(DAY) is None


def test_holiday_is_skipped_without_broker_book_reads() -> None:
    with Journal() as journal:
        cycle, _, broker, _ = build(journal, at=CLOSE)
        agent = Agent(
            cycle,
            journal,
            Settings(_env_file=None, ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret"),
            calendar_broker(trading_day=False),
        )
        assert agent.end_of_day(DAY, CLOSE) == {"skipped": True, "reason": "market_closed_day"}
        assert broker.account_calls == 0
        assert journal.last_reconciliation() is None


def test_broker_open_market_and_wrong_date_cannot_be_finalized() -> None:
    with Journal() as journal:
        cycle, _, _, _ = build(journal, at=CLOSE)
        agent = Agent(
            cycle,
            journal,
            Settings(_env_file=None, ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret"),
            calendar_broker(is_open=True),
        )
        with pytest.raises(RuntimeError, match="open market"):
            agent.end_of_day(DAY, CLOSE)
        with pytest.raises(ValueError, match="current session date"):
            agent.end_of_day(DAY - timedelta(days=1), CLOSE)
        assert journal.last_reconciliation() is None


def test_failed_observation_cannot_publish_final_reports() -> None:
    with Journal() as journal:
        cycle, _, broker, execution = build(journal, at=CLOSE)
        broker.positions_error = RuntimeError("broker unavailable")
        agent = Agent(
            cycle,
            journal,
            Settings(_env_file=None, ALPACA_API_KEY="test-key", ALPACA_SECRET_KEY="test-secret"),
            calendar_broker(),
        )
        with pytest.raises(RuntimeError, match="incomplete"):
            agent.end_of_day(DAY, CLOSE)
        assert journal.latest_pnl(trading_day=DAY) is None
        assert execution.calls == []
        assert journal.recent_decisions() == ()


class LookupBackend:
    name = Backend.SDK

    def __init__(self, outcome: BackendOutcome) -> None:
        self.outcome = outcome

    def unavailable_reason(self) -> str | None:
        return None

    def lookup(self, client_order_id: str) -> BackendOutcome:
        return self.outcome

    def submit(self, order: MultiLegOrder, *, dry_run: bool = False) -> BackendOutcome:
        raise AssertionError("The observation must not submit")

    def fetch_legs(self, order_id: str) -> tuple[LegFill, ...] | None:
        return None


@pytest.mark.parametrize("kind", [Kind.UNKNOWN, Kind.TERMINAL, Kind.ACCEPTED])
def test_unreadable_order_lookup_is_never_proven_absent(kind: Kind) -> None:
    reader = LiveOrderReader(ExecutionAdapter(primary=LookupBackend(BackendOutcome(kind))))
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        reader.order_status("pending")


def test_only_confirmed_absence_returns_none() -> None:
    reader = LiveOrderReader(ExecutionAdapter(primary=LookupBackend(BackendOutcome(Kind.ABSENT))))
    assert reader.order_status("pending") is None


def test_another_order_is_not_used_as_reconciliation_evidence() -> None:
    outcome = BackendOutcome(Kind.ACCEPTED, order=BrokerOrder("broker-id", "different", "filled"))
    reader = LiveOrderReader(ExecutionAdapter(primary=LookupBackend(outcome)))
    with pytest.raises(RuntimeError, match="different client order ID"):
        reader.order_status("pending")


def test_calendar_response_must_be_readable() -> None:
    broker = AlpacaBroker(SimpleNamespace(get_calendar=lambda _: None))
    with pytest.raises(ValueError, match="calendar response"):
        broker.is_trading_day(date(2026, 8, 31))


def test_real_sdk_reader_forwards_finite_network_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    timeouts = []

    def request(self: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        timeouts.append(kwargs.get("timeout"))
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = TradingClient(api_key="test-key", secret_key="test-secret", paper=True)
    bound_read_timeout(client)
    assert client.get_calendar(GetCalendarRequest(start=DAY, end=DAY)) == []
    assert timeouts == [(5, 20)]


def test_failed_close_status_is_visible_without_exposing_exception_text(tmp_path: Path) -> None:
    def fail(day: date, attempt: object) -> dict[str, object]:
        raise RuntimeError("private provider response must stay private")

    job = DailyCloseJob(directory=tmp_path, run_job=fail, clock=lambda: CLOSE)
    assert job.tick(CLOSE)
    with TestClient(
        create_app(DashboardConfig(end_of_day_dir=tmp_path, clock=lambda: CLOSE))
    ) as app:
        response = app.get("/api/end-of-day")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_type"] == "RuntimeError"
        assert body["retry_at"] is not None
        assert "private provider response" not in response.text
        assert str(tmp_path) not in response.text


def test_close_status_is_disabled_for_an_unconfigured_preview() -> None:
    with TestClient(create_app()) as app:
        response = app.get("/api/end-of-day")
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert response.json()["status"] == "disabled"
