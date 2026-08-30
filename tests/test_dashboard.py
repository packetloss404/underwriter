"""Dashboard API tests.

Three things are load-bearing here and each has a test that fails loudly.

**Nothing mutates.** The suite enumerates the router and asserts every route is
a GET, so an endpoint that could place an order, touch the kill switch or write
the journal breaks the build rather than the account. This is a hosted
submission requirement, not a preference.

**The empty journal is the important case.** We have not traded yet, so the
state a judge is most likely to load is the one with no rows in it. Every
endpoint has to render that without erroring and without inventing a zero where
it means "unknown".

**Nothing leaks.** Every response body is swept for credential-shaped strings,
including a deliberately planted fake key in a decision's recorded context.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from underwriter.dashboard import (
    DashboardConfig,
    JournalGateway,
    _safe_context,
    create_app,
    decisions_payload,
    pnl_payload,
    positions_payload,
    rejections_payload,
    state_payload,
)
from underwriter.journal import (
    MEMORY,
    FillSource,
    IntentLeg,
    Journal,
    KillSwitchActor,
    OrderStatus,
    PnlSource,
    PositionRecord,
    ReconciliationScope,
    Stage,
)

# 14:30 ET on a Friday: inside a session, so the ET trading day and the UTC
# date agree and every seeded row files under the day the tests assert on.
NOW = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)

SHORT_LEG = "XLE260911P00082000"
LONG_LEG = "XLE260911P00080000"
CLIENT_ORDER_ID = "uw-20260828-XLE-0001"

# Planted in a decision's context to prove redaction. If this string ever
# appears in a response body, something is publishing what it was handed.
PLANTED_SECRET = "sk-live-AAAABBBBCCCCDDDDEEEEFFFF"

# What "looks like a credential" means for the leak sweep.
CREDENTIAL_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bPK[A-Z0-9]{14,}\b"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
)


def config(
    *,
    journal_path: str | Path = MEMORY,
    static_dir: Path = Path("/nonexistent-static"),
    pnl_days: int = 30,
) -> DashboardConfig:
    """A config with the clock pinned, so every age in a response is exact."""
    return DashboardConfig(
        journal_path=journal_path,
        static_dir=static_dir,
        pnl_days=pnl_days,
        clock=lambda: NOW,
    )


@pytest.fixture
def journal() -> Iterator[Journal]:
    """An in-thread journal, for testing the payload builders directly."""
    with Journal(MEMORY) as opened:
        yield opened


@pytest.fixture
def gateway() -> Iterator[JournalGateway]:
    """A journal confined to its own thread, as the app uses it.

    Seeding goes through `gateway.run` for the same reason reads do: SQLite
    objects belong to the thread that made them, and the app's requests do not
    run on the test's thread.
    """
    made = JournalGateway(MEMORY)
    yield made
    made.close()


@pytest.fixture
def client(gateway: JournalGateway) -> Iterator[TestClient]:
    with TestClient(create_app(config(), gateway=gateway)) as opened:
        yield opened


def seed(journal: Journal) -> None:
    """One cycle's worth of history: a trade taken, and several refused."""
    journal.record_session_open_equity(equity=100_000.0, at=NOW - timedelta(hours=5))
    journal.record_reconciliation(
        scope=ReconciliationScope.FULL, ok=True, at=NOW - timedelta(minutes=2)
    )

    journal.record_decision(
        cycle_id="cyc-1",
        stage=Stage.RANK,
        accepted=True,
        symbol="XLE",
        reasons=(),
        detail=("iv_rank 62.0 over the 40.0 floor",),
        context={"iv_rank": 62.0, "api_key": PLANTED_SECRET},
        at=NOW - timedelta(minutes=30),
    )
    journal.record_decision(
        cycle_id="cyc-1",
        stage=Stage.RANK,
        accepted=False,
        symbol="XLU",
        reasons=("iv_rank_too_low",),
        detail=("iv_rank 18.0 under the 40.0 floor",),
        context={"iv_rank": 18.0},
        at=NOW - timedelta(minutes=29),
    )
    journal.record_decision(
        cycle_id="cyc-1",
        stage=Stage.RISK,
        accepted=False,
        symbol="XLF",
        reasons=("iv_rank_too_low", "spread_too_wide"),
        detail=("two reasons on one refusal",),
        at=NOW - timedelta(minutes=28),
    )
    journal.record_decision(
        cycle_id="cyc-1",
        stage=Stage.VETO,
        accepted=False,
        symbol="XLK",
        reasons=("veto_catalyst",),
        detail=("earnings inside the holding window",),
        at=NOW - timedelta(minutes=27),
    )

    journal.record_intent(
        client_order_id=CLIENT_ORDER_ID,
        cycle_id="cyc-1",
        symbol="XLE",
        spreads=2,
        payload={"limit_price": "-1.20", "qty": "2", "type": "limit"},
        legs=(
            IntentLeg(
                occ_symbol=SHORT_LEG, side="sell", ratio_qty=1, position_intent="sell_to_open"
            ),
            IntentLeg(occ_symbol=LONG_LEG, side="buy", ratio_qty=1, position_intent="buy_to_open"),
        ),
        at=NOW - timedelta(minutes=26),
    )
    journal.mark_submitted(CLIENT_ORDER_ID, broker_order_id="brk-1", at=NOW - timedelta(minutes=25))
    journal.mark_status(
        CLIENT_ORDER_ID,
        OrderStatus.FILLED,
        spreads_filled=2,
        net_price_per_spread=-1.20,
        at=NOW - timedelta(minutes=24),
    )
    journal.record_spread_fill(
        fill_id="fill-1",
        symbol="XLE",
        spreads=2,
        net_price_per_spread=-1.20,
        occurred_at=NOW - timedelta(minutes=24),
        source=FillSource.REST,
        client_order_id=CLIENT_ORDER_ID,
    )
    journal.record_leg_fill(
        fill_id="leg-fill-1",
        occ_symbol=SHORT_LEG,
        contracts=2,
        premium_per_contract=2.10,
        side="sell",
        occurred_at=NOW - timedelta(minutes=24),
        source=FillSource.REST,
        parent_fill_id="fill-1",
        client_order_id=CLIENT_ORDER_ID,
    )
    journal.record_leg_fill(
        fill_id="leg-fill-2",
        occ_symbol=LONG_LEG,
        contracts=2,
        premium_per_contract=0.90,
        side="buy",
        occurred_at=NOW - timedelta(minutes=24),
        source=FillSource.REST,
        parent_fill_id="fill-1",
        client_order_id=CLIENT_ORDER_ID,
    )

    journal.record_position_snapshot(
        (
            PositionRecord(
                symbol="XLE",
                spreads=2,
                max_loss=160.0,
                unrealised_pnl=48.0,
                net_delta=-24.0,
                client_order_id=CLIENT_ORDER_ID,
            ),
        ),
        at=NOW - timedelta(minutes=3),
    )

    journal.record_pnl(
        source=PnlSource.OFFICIAL,
        realised_pnl=0.0,
        unrealised_pnl=48.0,
        equity=100_240.0,
        at=NOW - timedelta(minutes=3),
    )
    journal.record_pnl(
        source=PnlSource.SHADOW,
        realised_pnl=0.0,
        unrealised_pnl=31.0,
        equity=100_223.0,
        detail="exits priced across the quoted spread",
        at=NOW - timedelta(minutes=3),
    )


@pytest.fixture
def populated(gateway: JournalGateway, client: TestClient) -> TestClient:
    gateway.run(seed)
    return client


ROUTES = (
    "/",
    # Brand assets, served through a flat allow-list rather than a mounted
    # directory: the static folder sits inside the installed package, and a
    # traversal bug would read arbitrary files out of a container that also
    # holds broker credentials in its environment.
    "/ledger",
    "/static/{name}",
    "/favicon.ico",
    "/api/health",
    "/api/state",
    "/api/positions",
    "/api/decisions",
    "/api/rejections",
    "/api/pnl",
    "/api/orders",
)

# Derived positively rather than by exclusion: every non-JSON route added so
# far broke this list, because a subtractive filter has to be updated for
# each new kind of route while an additive one does not.
JSON_ROUTES = tuple(r for r in ROUTES if r.startswith("/api/"))


class TestReadOnly:
    """The safety property. Everything else is a feature; this is the promise."""

    def test_every_route_is_a_get(self) -> None:
        app = create_app(config())
        checked = 0
        for route in app.routes:
            assert isinstance(route, (APIRoute, Route)), f"unexpected route type: {route!r}"
            methods = route.methods or set()
            assert methods <= {"GET", "HEAD"}, f"{route.path} exposes {sorted(methods)}"
            checked += 1
        assert checked >= len(ROUTES)

    def test_no_route_accepts_a_mutation(self, client: TestClient) -> None:
        for path in ROUTES:
            for verb in ("post", "put", "patch", "delete"):
                response = getattr(client, verb)(path)
                assert response.status_code == 405, f"{verb.upper()} {path} was not refused"

    def test_openapi_document_declares_only_gets(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert paths
        for path, operations in paths.items():
            assert set(operations) <= {"get"}, f"{path} documents {sorted(operations)}"

    def test_the_route_set_is_exactly_what_was_declared(self) -> None:
        """A new route has to be added here as well, deliberately, in a review."""
        app = create_app(config())
        declared = {
            route.path
            for route in app.routes
            if isinstance(route, (APIRoute, Route)) and not route.path.startswith("/docs")
        }
        assert declared == {
            *ROUTES,
            "/openapi.json",
            "/redoc",
        }


class TestEmptyJournal:
    """The current state of the system, and therefore the most important one."""

    def test_every_route_answers(self, client: TestClient) -> None:
        # ROUTES carries route *shapes* for the pinning test. The templated
        # static route is not a fetchable path, and the asset routes answer
        # from disk rather than from the journal -- this fixture points
        # static_dir at a nonexistent path on purpose, so a 404 there is
        # correct. TestBrandAssets covers those against the real directory.
        for path in ("/", *JSON_ROUTES):
            assert client.get(path).status_code == 200, path

    def test_state_reports_gaps_rather_than_failing(self, client: TestClient) -> None:
        body = client.get("/api/state").json()
        assert body["kill_switch"]["engaged"] is False
        assert body["view_stale"] is True
        assert body["reconciliation"]["never_reconciled"] is True
        assert "session_equity_missing" in body["recovery"]["gaps"]
        assert body["session_open_equity_usd"] is None
        assert body["book"]["observed"] is False
        assert body["book"]["open_positions"] == 0

    def test_positions_are_empty_not_absent(self, client: TestClient) -> None:
        body = client.get("/api/positions").json()
        assert body["observed"] is False
        assert body["positions"] == []
        assert body["count"] == 0
        assert body["totals"]["max_loss_usd"] == 0

    def test_decisions_and_rejections_are_empty_structures(self, client: TestClient) -> None:
        decisions = client.get("/api/decisions").json()
        assert decisions["decisions"] == []
        assert decisions["accepted"] == 0

        rejections = client.get("/api/rejections").json()
        assert rejections["total_rejections"] == 0
        assert rejections["by_reason"] == []
        assert rejections["by_stage"] == []
        # No decisions examined means no rate, not a rate of zero.
        assert rejections["refusal_rate_pct"] is None

    def test_pnl_has_both_series_with_no_points(self, client: TestClient) -> None:
        series = client.get("/api/pnl").json()["series"]
        assert set(series) == {"official", "shadow"}
        for source in series.values():
            assert source["points"] == []
            assert source["latest"] is None

    def test_orders_are_empty(self, client: TestClient) -> None:
        body = client.get("/api/orders").json()
        assert body["orders"] == []
        assert body["unreconciled"] == 0

    def test_data_age_is_null_rather_than_zero(self, client: TestClient) -> None:
        for path in JSON_ROUTES:
            body = client.get(path).json()
            assert body["data_age_seconds"] is None, path


class TestStaleness:
    def test_every_response_carries_generation_and_age(self, populated: TestClient) -> None:
        for path in JSON_ROUTES:
            body = populated.get(path).json()
            assert body["generated_at"] == NOW.isoformat(), path
            assert "data_as_of" in body, path
            assert "data_age_seconds" in body, path

    def test_age_is_measured_from_the_newest_datum(self, populated: TestClient) -> None:
        body = populated.get("/api/positions").json()
        assert body["data_age_seconds"] == pytest.approx(180.0)

    def test_a_fresh_reconciliation_clears_the_stale_flag(
        self, gateway: JournalGateway, client: TestClient
    ) -> None:
        gateway.run(seed)
        assert client.get("/api/state").json()["view_stale"] is False

    def test_an_old_reconciliation_reads_as_stale(
        self, gateway: JournalGateway, client: TestClient
    ) -> None:
        gateway.run(
            lambda journal: journal.record_reconciliation(
                scope=ReconciliationScope.FULL, ok=True, at=NOW - timedelta(hours=2)
            )
        )
        body = client.get("/api/state").json()
        assert body["view_stale"] is True
        assert body["reconciliation"]["age_seconds"] == pytest.approx(7200.0)


class TestPopulatedShapes:
    def test_state_shows_the_open_book(self, populated: TestClient) -> None:
        body = populated.get("/api/state").json()
        assert body["book"]["observed"] is True
        assert body["book"]["open_positions"] == 1
        assert body["session_open_equity_usd"] == 100_000.0
        assert body["realised_pnl_today_usd"] == 0.0
        assert body["attention"]["unconfirmed_fills"] == 0

    def test_kill_switch_is_reported_with_actor_and_reason(
        self, gateway: JournalGateway, client: TestClient
    ) -> None:
        gateway.run(
            lambda journal: journal.engage_kill_switch(
                reason="daily loss stop tripped",
                actor=KillSwitchActor.RISK,
                at=NOW - timedelta(minutes=10),
            )
        )
        switch = client.get("/api/state").json()["kill_switch"]
        assert switch["engaged"] is True
        assert switch["reason"] == "daily loss stop tripped"
        assert switch["actor"] == "risk"
        assert switch["age_seconds"] == pytest.approx(600.0)
        assert client.get("/api/state").json()["may_trade"] is False

    def test_position_is_joined_to_its_order_and_legs(self, populated: TestClient) -> None:
        body = populated.get("/api/positions").json()
        assert body["observed"] is True
        position = body["positions"][0]
        assert position["underlying"] == "XLE"
        assert position["spreads"] == 2
        assert position["max_loss_usd"] == 160.0
        assert position["unrealised_pnl_usd"] == 48.0
        assert position["net_delta"] == -24.0
        assert position["mapped_to_order"] is True
        assert position["expiry"] == "2026-09-11"
        assert position["days_to_expiry"] == 14
        assert {leg["occ_symbol"] for leg in position["legs"]} == {SHORT_LEG, LONG_LEG}

    def test_an_unmapped_position_still_renders(
        self, gateway: JournalGateway, client: TestClient
    ) -> None:
        gateway.run(
            lambda journal: journal.record_position_snapshot(
                (PositionRecord(symbol="XLU", spreads=1, max_loss=90.0, detail="orphan"),),
                at=NOW - timedelta(minutes=1),
            )
        )
        position = client.get("/api/positions").json()["positions"][0]
        assert position["mapped_to_order"] is False
        assert position["legs"] == []
        assert position["expiry"] is None
        assert position["days_to_expiry"] is None
        assert position["credit_per_spread_usd"] is None

    def test_decisions_carry_reasons_and_detail(self, populated: TestClient) -> None:
        body = populated.get("/api/decisions").json()
        assert body["accepted"] == 1
        assert body["rejected"] == 3
        rejected = [d for d in body["decisions"] if not d["accepted"]]
        assert all(d["reasons"] for d in rejected), "a rejection must name itself"
        assert body["decisions"][0]["stage"] == "veto"

    def test_decisions_filter_by_cycle_and_symbol(self, populated: TestClient) -> None:
        assert populated.get("/api/decisions?cycle_id=cyc-1").json()["window"]["returned"] == 4
        assert populated.get("/api/decisions?cycle_id=nope").json()["decisions"] == []
        body = populated.get("/api/decisions?symbol=XLU").json()
        assert [d["symbol"] for d in body["decisions"]] == ["XLU"]

    def test_orders_expose_status_and_fills(self, populated: TestClient) -> None:
        body = populated.get("/api/orders").json()
        order = body["orders"][0]
        assert order["client_order_id"] == CLIENT_ORDER_ID
        assert order["status"] == "filled"
        assert order["is_terminal"] is True
        assert order["spreads_ordered"] == 2
        assert order["spreads_filled"] == 2
        assert order["spreads_working"] == 0
        assert order["partially_filled"] is False
        assert body["by_status"] == [{"status": "filled", "count": 1}]
        assert len(order["fills"]) == 1

    def test_orders_never_publish_the_broker_payload(self, populated: TestClient) -> None:
        order = populated.get("/api/orders").json()["orders"][0]
        assert "payload" not in order

    def test_health_reports_the_journal(self, populated: TestClient) -> None:
        body = populated.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["read_only"] is True
        assert body["journal_readable"] is True

    def test_index_falls_back_when_the_page_is_not_built(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "/api/rejections" in response.text

    def test_index_serves_the_static_page_when_it_exists(
        self, gateway: JournalGateway, tmp_path: Path
    ) -> None:
        (tmp_path / "index.html").write_text("<h1>underwriter</h1>", encoding="utf-8")
        app = create_app(config(static_dir=tmp_path), gateway=gateway)
        with TestClient(app) as serving:
            response = serving.get("/")
            assert response.status_code == 200
            assert "underwriter" in response.text


class TestRejectionsAreTheHeadline:
    def test_reasons_are_grouped_and_counted(self, populated: TestClient) -> None:
        body = populated.get("/api/rejections").json()
        assert body["total_rejections"] == 3
        groups = {group["reason"]: group for group in body["by_reason"]}
        assert groups["iv_rank_too_low"]["count"] == 2
        assert groups["iv_rank_too_low"]["symbols"] == ["XLF", "XLU"]
        assert groups["spread_too_wide"]["count"] == 1
        assert groups["veto_catalyst"]["stages"] == ["veto"]

    def test_groups_are_ordered_by_count(self, populated: TestClient) -> None:
        counts = [group["count"] for group in populated.get("/api/rejections").json()["by_reason"]]
        assert counts == sorted(counts, reverse=True)

    def test_multi_reason_rejections_are_declared_not_hidden(self, populated: TestClient) -> None:
        body = populated.get("/api/rejections").json()
        assert sum(group["count"] for group in body["by_reason"]) == 4
        assert body["total_rejections"] == 3
        assert "sum to at least total_rejections" in body["counting"]

    def test_refusal_rate_is_over_a_stated_window(self, populated: TestClient) -> None:
        body = populated.get("/api/rejections").json()
        assert body["decisions_examined"] == 4
        assert body["accepted_in_decisions_examined"] == 1
        assert body["refusal_rate_pct"] == pytest.approx(75.0)

    def test_stage_breakdown_is_present(self, populated: TestClient) -> None:
        stages = {
            row["stage"]: row["count"]
            for row in populated.get("/api/rejections").json()["by_stage"]
        }
        assert stages == {"rank": 1, "risk": 1, "veto": 1}

    def test_every_rejection_still_carries_its_own_row(self, populated: TestClient) -> None:
        recent = populated.get("/api/rejections").json()["recent"]
        assert len(recent) == 3
        assert all(row["reasons"] for row in recent)


class TestUnits:
    """docs/GOTCHAS.md #8: parent and leg speak different languages."""

    def test_every_money_field_is_labelled(self, populated: TestClient) -> None:
        for path in ("/api/positions", "/api/orders", "/api/pnl"):
            units = populated.get(path).json()["units"]
            assert units, path
            assert "spreads" in units or "realised_pnl_usd" in units, path

    def test_dollar_fields_name_their_unit_in_the_key(self, populated: TestClient) -> None:
        position = populated.get("/api/positions").json()["positions"][0]
        for money in ("max_loss_usd", "unrealised_pnl_usd", "credit_per_spread_usd"):
            assert money in position
        # The signed net price is per share and says so by name rather than
        # pretending to be a dollar total.
        assert position["net_price_per_spread_signed"] == -1.20
        assert position["credit_per_spread_usd"] == pytest.approx(120.0)
        assert position["credit_received_usd"] == pytest.approx(240.0)

    def test_parent_and_leg_fills_keep_their_own_units(self, populated: TestClient) -> None:
        fill = populated.get("/api/orders").json()["orders"][0]["fills"][0]
        # Parent: strategy units, signed net price, negative for a credit.
        assert fill["spreads"] == 2
        assert fill["net_price_per_spread_signed"] == -1.20
        assert fill["credit_received_usd"] == pytest.approx(240.0)
        # Legs: contracts, and each leg's own positive premium.
        premiums = {leg["occ_symbol"]: leg["premium_per_contract_usd"] for leg in fill["legs"]}
        assert premiums == {SHORT_LEG: 2.10, LONG_LEG: 0.90}
        assert all(leg["contracts"] == 2 for leg in fill["legs"])
        assert all(leg["premium_per_contract_usd"] > 0 for leg in fill["legs"])

    def test_leg_contract_count_is_the_ratio_times_the_spreads(self, populated: TestClient) -> None:
        position = populated.get("/api/positions").json()["positions"][0]
        for leg in position["legs"]:
            assert leg["contracts"] == leg["ratio_qty"] * position["spreads"]


class TestPnlHonesty:
    """docs/GOTCHAS.md #3: the official paper number is reported, not trusted."""

    def test_the_two_series_are_separate(self, populated: TestClient) -> None:
        series = populated.get("/api/pnl").json()["series"]
        assert series["official"]["latest"]["unrealised_pnl_usd"] == 48.0
        assert series["shadow"]["latest"]["unrealised_pnl_usd"] == 31.0

    def test_nothing_sums_them(self, populated: TestClient) -> None:
        body = populated.get("/api/pnl").json()
        flat = str(body)
        assert "total_pnl" not in flat
        assert "combined" not in flat
        assert "never_summed" in body

    def test_official_is_flagged_untrusted(self, populated: TestClient) -> None:
        series = populated.get("/api/pnl").json()["series"]
        assert series["official"]["trusted"] is False
        assert series["shadow"]["trusted"] is True
        assert "GOTCHAS" in series["official"]["note"]

    def test_window_is_stated(self, populated: TestClient) -> None:
        body = populated.get("/api/pnl?days=7").json()
        assert body["window"]["days"] == 7
        assert body["window"]["to_trading_day"] == "2026-08-28"
        assert body["window"]["from_trading_day"] == "2026-08-22"

    def test_days_without_a_snapshot_are_absent_not_zero(self, populated: TestClient) -> None:
        points = populated.get("/api/pnl?days=30").json()["series"]["official"]["points"]
        assert len(points) == 1
        assert points[0]["trading_day"] == "2026-08-28"


class TestNoLeaks:
    def test_no_response_body_looks_like_a_credential(self, populated: TestClient) -> None:
        for path in ROUTES:
            body = populated.get(path).text
            assert PLANTED_SECRET not in body, path
            for shape in CREDENTIAL_SHAPES:
                assert shape.search(body) is None, f"{path} matched {shape.pattern}"

    def test_recorded_context_is_redacted_by_key(self, populated: TestClient) -> None:
        decisions = populated.get("/api/decisions").json()["decisions"]
        accepted = next(d for d in decisions if d["accepted"])
        assert accepted["context"]["api_key"] == "[redacted]"
        assert accepted["context"]["iv_rank"] == 62.0

    def test_redaction_reaches_nested_values(self) -> None:
        cleaned = _safe_context(
            {
                "nested": {"authorization": "Bearer abcdefghijkl", "width": 2.0},
                "values": ["sk-live-1234567890abcdef", "XLE"],
                "plain": 1,
            }
        )
        assert cleaned == {
            "nested": {"authorization": "[redacted]", "width": 2.0},
            "values": ["[redacted]", "XLE"],
            "plain": 1,
        }

    def test_an_unreadable_journal_reports_503_without_its_path(self, tmp_path: Path) -> None:
        """A journal we cannot read is unavailable, not empty -- and stays quiet.

        The `JournalError` message names the database file. This app is the part
        of the system a stranger can reach, so the message is withheld and only
        the error type is published.
        """
        intruder = tmp_path / "someone-elses.db"
        with sqlite3.connect(intruder) as conn:
            conn.execute("CREATE TABLE payroll (id INTEGER PRIMARY KEY)")
        app = create_app(config(journal_path=intruder))
        with TestClient(app, raise_server_exceptions=False) as broken:
            response = broken.get("/api/state")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["error"] == "JournalError"
        assert str(tmp_path) not in response.text
        assert "payroll" not in response.text


class TestBuildersDirectly:
    """The payload builders are pure reads, so they are testable without HTTP."""

    def test_state_of_an_untouched_journal(self, journal: Journal) -> None:
        body = state_payload(journal, now=NOW, max_view_age=timedelta(minutes=5))
        assert body["trading_day"] == "2026-08-28"
        assert body["view_stale"] is True

    def test_builders_do_not_write(self, journal: Journal) -> None:
        seed(journal)
        before = (
            len(journal.recent_decisions(100)),
            len(journal.order_history(100)),
            journal.latest_positions().id,
        )
        state_payload(journal, now=NOW, max_view_age=timedelta(minutes=5))
        positions_payload(journal, now=NOW)
        decisions_payload(journal, now=NOW, limit=50)
        rejections_payload(journal, now=NOW, limit=50)
        pnl_payload(journal, now=NOW, days=5)
        after = (
            len(journal.recent_decisions(100)),
            len(journal.order_history(100)),
            journal.latest_positions().id,
        )
        assert before == after

    def test_limits_are_honoured_and_declared(self, journal: Journal) -> None:
        seed(journal)
        body = decisions_payload(journal, now=NOW, limit=2)
        assert body["window"] == {
            "limit": 2,
            "returned": 2,
            "truncated": True,
            "cycle_id": None,
            "symbol": None,
        }

    def test_limits_are_bounded_by_the_api(self, client: TestClient) -> None:
        assert client.get("/api/decisions?limit=0").status_code == 422
        assert client.get("/api/decisions?limit=100000").status_code == 422
        assert client.get("/api/pnl?days=0").status_code == 422


class TestBrandAssets:
    """Assets are allow-listed, not directory-mounted.

    The static folder lives inside the installed package, so a traversal bug
    here would read arbitrary files out of a container whose environment holds
    broker credentials. An allow-list cannot traverse.
    """

    @pytest.fixture
    def client(self) -> TestClient:
        # The shared fixture points static_dir at a nonexistent path on
        # purpose; these tests need the real assets.
        import underwriter

        real = Path(underwriter.__file__).resolve().parent / "static"
        return TestClient(create_app(config(static_dir=real)))

    def test_a_known_asset_is_served(self, client: TestClient) -> None:
        response = client.get("/static/logo-mark.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    @pytest.mark.parametrize(
        "path",
        [
            "/static/../dashboard.py",
            "/static/..%2Fdashboard.py",
            "/static/index.html",
            "/static/journal.py",
            "/static/nope.png",
        ],
    )
    def test_anything_not_allow_listed_is_refused(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 404

    def test_assets_are_still_read_only(self, client: TestClient) -> None:
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(client, verb)("/static/logo-mark.png").status_code == 405
