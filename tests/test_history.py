"""Full-history counts, ET boundaries, independent equity series and stable pages."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_dashboard import NOW, config, seed
from underwriter.dashboard import JournalGateway, create_app
from underwriter.journal import Journal, PnlSource, ReconciliationScope, Stage


def record(journal: Journal, i: int, *, at: datetime = NOW) -> None:
    journal.record_decision(
        cycle_id=f"cycle-{i}",
        stage=Stage.RANK,
        accepted=False,
        symbol="SPY",
        reasons=("premium_below_floor",),
        at=at,
    )


def test_counts_cover_more_than_the_recent_row_cap() -> None:
    with Journal() as j:
        for i in range(1_107):
            record(j, i, at=NOW - timedelta(days=40) + timedelta(minutes=i))
        body = j.history_summary(today=NOW.date())
        assert body["totals"]["decisions"] == 1_107
        assert body["totals"]["cycles"] == 1_107
        assert sum(d["decisions"] for d in body["daily"]) == 1_107
        assert body["coverage"]["inventory"]["decisions"] == 1_107
        assert body["reasons"][0]["count"] == 1_107
        assert body["range"]["complete"] is True


def test_et_days_and_inclusive_end_match_archive() -> None:
    with Journal() as j:
        record(j, 1, at=datetime(2026, 9, 2, 2, tzinfo=UTC))  # Sep 1 ET
        record(j, 2, at=datetime(2026, 9, 2, 4, tzinfo=UTC))  # Sep 2 ET
        start = end = date(2026, 9, 1)
        summary = j.history_summary(start=start, end=end, today=NOW.date())
        rows, page = j.history_decisions(start=start, end=end)
        assert summary["totals"]["decisions"] == page["total"] == 1
        assert rows[0].cycle_id == "cycle-1"
        assert summary["daily"][0]["date"] == "2026-09-01"


def test_dst_day_is_not_assumed_to_be_24_hours() -> None:
    with Journal() as j:
        record(j, 1, at=datetime(2026, 11, 1, 4, tzinfo=UTC))
        record(j, 2, at=datetime(2026, 11, 2, 4, 59, tzinfo=UTC))
        record(j, 3, at=datetime(2026, 11, 2, 5, tzinfo=UTC))
        b = j.history_summary(start=date(2026, 11, 1), end=date(2026, 11, 1), today=NOW.date())
        assert b["totals"]["decisions"] == 2


def test_failures_exploration_and_multi_reason_decisions_remain_distinct() -> None:
    with Journal() as j:
        for stage, reasons, detail in [
            (Stage.VETO, ("veto_catalyst",), ("Model unavailable (APIConnectionError)",)),
            (Stage.VETO, ("veto_catalyst",), ("Identified sector event",)),
            (Stage.SCAN, ("no_viable_width", "reward_risk_too_low"), ()),
            (Stage.EXPLORE, ("premium_below_floor",), ()),
        ]:
            j.record_decision(
                cycle_id="one", stage=stage, accepted=False, reasons=reasons, detail=detail, at=NOW
            )
        b = j.history_summary(today=NOW.date())
        assert b["totals"]["decisions"] == 4
        assert b["totals"]["live_decisions"] == 3
        assert b["totals"]["operational"] == 1
        assert b["totals"]["cycles"] == 1
        assert sum(r["count"] for r in b["reasons"]) == 4
        assert {r["category"] for r in b["reasons"]} == {
            "model_unavailable",
            "veto_catalyst",
            "no_viable_width",
            "reward_risk_too_low",
        }
        rows, _ = j.history_decisions(reason="model_unavailable")
        assert len(rows) == 1
        rows, _ = j.history_decisions(outcome="operational")
        assert len(rows) == 1


def test_live_failure_drilldown_excludes_exploratory_failures() -> None:
    gateway = JournalGateway(":memory:")
    try:

        def populate(journal: Journal) -> None:
            for stage in (Stage.VETO, Stage.EXPLORE):
                journal.record_decision(
                    cycle_id="one",
                    stage=stage,
                    accepted=False,
                    symbol="SPY",
                    reasons=("veto_catalyst",),
                    detail=("Model unavailable",),
                    at=NOW,
                )

        gateway.run(populate)
        with TestClient(create_app(config(), gateway=gateway)) as client:
            summary = client.get("/api/history").json()
            for track, stage in (("live", "veto"), ("exploratory", "explore")):
                response = client.get(
                    "/api/history/decisions",
                    params={
                        "track": track,
                        "reason": "model_unavailable",
                        "outcome": "rejected",
                    },
                )
                assert response.status_code == 200
                body = response.json()
                assert body["page"]["total"] == summary["reasons"][0]["count"] == 1
                assert body["decisions"][0]["stage"] == stage
            assert client.get("/api/history/decisions").json()["page"]["total"] == 2
    finally:
        gateway.close()


def test_last_equity_snapshot_is_not_summed_or_mixed_with_other_sources() -> None:
    with Journal() as j:
        for minutes, source, equity, pnl in [
            (0, PnlSource.OFFICIAL, 100_000, 0),
            (1, PnlSource.OFFICIAL, 100_015, 15),
            (2, PnlSource.SHADOW, 99_990, -10),
        ]:
            j.record_pnl(
                source=source,
                equity=equity,
                realised_pnl=pnl,
                unrealised_pnl=0,
                at=NOW + timedelta(minutes=minutes),
            )
        b = j.history_summary(today=NOW.date())
        assert b["totals"]["pnl_snapshots"] == 3
        official = next(x for x in b["equity"] if x["source"] == "official")
        assert official["equity"] == 100_015
        assert official["realised_pnl"] == 15
        assert b["equity_endpoints"]["official"]["first"]["equity"] == 100_000
        assert b["equity_endpoints"]["shadow"]["last"]["equity"] == 99_990
        assert b["equity_endpoints"]["exploratory"]["last"] is None


def test_calendar_gaps_have_no_invented_equity_or_position_measurement() -> None:
    with Journal() as j:
        record(j, 1)
        b = j.history_summary(
            start=NOW.date(), end=NOW.date() + timedelta(days=2), today=NOW.date()
        )
        assert len(b["daily"]) == 3
        assert b["daily"][1]["decisions"] == 0
        assert b["daily"][1]["max_open_positions"] is None
        assert b["equity"] == []


def test_pages_are_complete_with_equal_timestamps_and_concurrent_inserts() -> None:
    with Journal() as j:
        for i in range(125):
            record(j, i)
        first, page = j.history_decisions(limit=50)
        record(j, 999)
        ids = [r.id for r in first]
        while page["has_more"]:
            rows, page = j.history_decisions(
                limit=50, before=page["next_cursor"], snapshot=page["snapshot"]
            )
            assert page["total"] == 125
            ids.extend(r.id for r in rows)
        assert len(ids) == len(set(ids)) == 125
        assert ids == sorted(ids, reverse=True)
        rows, page = j.history_decisions(query="cycle-999")
        assert page["total"] == 1
        assert rows[0].cycle_id == "cycle-999"


def test_order_fill_and_position_totals_are_not_confused_with_stage_acceptance() -> None:
    with Journal() as j:
        seed(j)
        b = j.history_summary(today=NOW.date())
        assert b["totals"]["orders"] == 1
        assert b["totals"]["spread_fills"] == 1
        assert b["totals"]["position_snapshots"] == 1
        assert b["daily"][0]["max_open_positions"] == 1
        assert b["totals"]["exploratory_positions"] == 0


def test_long_ranges_reduce_chart_resolution_without_dropping_history() -> None:
    with Journal() as j:
        for i in range(3):
            record(j, i, at=NOW - timedelta(days=250 * i))
        b = j.history_summary(today=NOW.date())
        assert b["range"]["resolution"] == "week"
        assert sum(d["decisions"] for d in b["daily"]) == 3
        assert len(b["daily"]) < 100


@pytest.mark.parametrize("query", ["start=2026-09-01&end=2026-08-01", "end=9999-12-31"])
def test_bad_ranges_return_validation_errors(query: str) -> None:
    gateway = JournalGateway(":memory:")
    try:
        with TestClient(create_app(config(), gateway=gateway)) as client:
            for route in ("/api/history", "/api/history/decisions", "/api/reports"):
                assert client.get(route + "?" + query).status_code == 422
    finally:
        gateway.close()


def test_reports_count_affected_cycles_once_and_exclude_exploratory_failures() -> None:
    with Journal() as j:
        for cycle, stage, reasons in [
            ("failed", Stage.VETO, ("premium_below_floor", "preflight_missing")),
            ("failed", Stage.VETO, ("veto_catalyst",)),
            ("explore-only", Stage.EXPLORE, ("veto_catalyst",)),
        ]:
            j.record_decision(
                cycle_id=cycle,
                stage=stage,
                accepted=False,
                reasons=reasons,
                detail=("Model unavailable",),
                at=NOW,
            )
        record(j, 4)
        report = j.reports_summary(today=NOW.date())["report"]
        assert report["metrics"]["cycles"] == 3
        assert report["metrics"]["affected_cycles"] == 1
        assert report["metrics"]["operational"] == 2
        assert report["metrics"]["strategy_rejections"] == 1
        assert report["metrics"]["affected_cycle_pct"] == pytest.approx(100 / 3)
        assert report["operational"][0]["decisions"] == 2
        assert report["operational"][0]["cycles"] == 1
        assert len(report["operational"]) == 1  # multiple codes never double-count failures
        assert report["cycle_timeline"][0]["affected_cycles"] == 1
        assert report["strategy_reasons"] == [
            {
                "reason": "premium_below_floor",
                "count": 1,
                "first_at": NOW.isoformat(timespec="microseconds"),
                "last_at": NOW.isoformat(timespec="microseconds"),
            }
        ]
        rows, page = j.history_decisions(outcome="strategy", reason="premium_below_floor")
        assert page["total"] == 1
        assert rows[0].cycle_id == "cycle-4"


def test_reports_equal_calendar_comparison_is_complete_and_not_a_zero_baseline() -> None:
    with Journal() as j:
        record(j, 1, at=NOW - timedelta(days=3))
        record(j, 2, at=NOW - timedelta(days=2))
        record(j, 3, at=NOW - timedelta(days=1))
        record(j, 4)
        body = j.reports_summary(
            start=NOW.date() - timedelta(days=1), end=NOW.date(), today=NOW.date()
        )
        current, prior = body["report"]["metrics"], body["report"]["comparison"]
        assert current["cycles"] == prior["metrics"]["cycles"] == 2
        assert prior["range"] == {
            "start": (NOW.date() - timedelta(days=3)).isoformat(),
            "end": (NOW.date() - timedelta(days=2)).isoformat(),
            "calendar_days": 2,
        }
        assert prior["available"] is True
        whole_run = j.reports_summary(today=NOW.date())["report"]
        assert whole_run["comparison"]["available"] is False
        assert whole_run["comparison"]["metrics"]["decisions"] == 0


def test_reports_preserve_unknown_money_and_independent_series() -> None:
    with Journal() as j:
        j.record_pnl(
            source=PnlSource.OFFICIAL, equity=100_000, realised_pnl=0, unrealised_pnl=0, at=NOW
        )
        j.record_pnl(
            source=PnlSource.SHADOW, equity=None, realised_pnl=12, unrealised_pnl=-3, at=NOW
        )
        report = j.reports_summary(today=NOW.date())["report"]
        official, shadow = report["pnl_sources"]
        assert official["source"] == "official"
        assert official["latest"]["equity"] == 100_000
        assert shadow["equity_observations"] == 0
        assert shadow["latest"]["equity"] is None
        assert shadow["latest"]["realised_pnl"] == 12
        assert shadow["latest"]["unrealised_pnl"] == -3
        assert report["metrics"]["affected_cycle_pct"] is None


def test_reports_reconciliation_regime_and_execution_keep_their_own_units() -> None:
    with Journal() as j:
        seed(j)
        j.record_reconciliation(scope=ReconciliationScope.FULL, ok=False, at=NOW)
        j.record_regime_verdict(allowed=False, blocks=("vix_high", "credit_stress"), at=NOW)
        report = j.reports_summary(today=NOW.date())["report"]
        assert report["order_statuses"][0]["orders"] == 1
        assert report["fill_sources"][0]["fills"] == 1
        assert sum(x["failed"] for x in report["reconciliation"]) == 1
        assert {x["reason"] for x in report["regime_blocks"]} >= {"vix_high", "credit_stress"}
        coverage = {x["table"]: x for x in report["coverage"]}
        assert coverage["orders"]["count"] == coverage["orders"]["all_time"] == 1
        assert coverage["decisions"]["first_at"] is not None


def test_report_at_start_of_supported_calendar_has_no_wrapped_prior_period() -> None:
    with Journal() as j:
        report = j.reports_summary(start=date(1, 1, 2), end=date(1, 1, 3), today=NOW.date())
        assert report["report"]["comparison"] == {
            "available": False,
            "range": None,
            "metrics": None,
        }


@pytest.mark.parametrize("year,resolution", [(9998, "week"), (9995, "month")])
def test_long_ranges_can_end_near_the_last_supported_calendar_day(
    year: int, resolution: str
) -> None:
    with Journal() as j:
        body = j.history_summary(start=date(year, 1, 1), end=date(9999, 12, 30), today=NOW.date())
        assert body["range"]["resolution"] == resolution
        assert body["daily"][-1]["date"].startswith("9999-12")
        assert body["totals"]["decisions"] == 0
