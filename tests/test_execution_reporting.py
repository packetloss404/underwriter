"""Public evidence preserves execution provenance and distinct parent/leg units."""

import json
from pathlib import Path

from tests.test_dashboard import LONG_LEG, NOW, SHORT_LEG
from underwriter.dashboard import _execution_provenance
from underwriter.journal import FillSource, IntentLeg, Journal, OrderStatus


def opening(journal: Journal) -> None:
    journal.record_intent(
        client_order_id="report-execution",
        cycle_id="report-evidence",
        symbol="XLE",
        spreads=2,
        payload={},
        legs=(
            IntentLeg(SHORT_LEG, "sell", 1, "sell_to_open"),
            IntentLeg(LONG_LEG, "buy", 1, "buy_to_open"),
        ),
        at=NOW,
    )
    journal.mark_submitted("report-execution", broker_order_id="parent-order", at=NOW)
    journal.mark_status(
        "report-execution",
        OrderStatus.FILLED,
        spreads_filled=2,
        net_price_per_spread=-1.2,
        at=NOW,
    )


def parent(journal: Journal) -> None:
    journal.record_spread_fill(
        fill_id="broker-activity-short",
        client_order_id="report-execution",
        broker_order_id="parent-order",
        symbol="XLE",
        spreads=2,
        net_price_per_spread=-1.2,
        source=FillSource.REST,
        occurred_at=NOW,
        at=NOW,
        detail=json.dumps(
            {
                "kind": "alpaca_leg_activity_aggregate_v1",
                "activity_ids": ["broker-activity-short", "broker-activity-long"],
                "timestamp_semantics": "latest_component_execution_time",
            }
        ),
    )


def leg(journal: Journal, *, linked: bool = True) -> None:
    for activity, symbol, side, price in (
        ("broker-activity-short", SHORT_LEG, "sell", 1.3),
        ("broker-activity-long", LONG_LEG, "buy", 0.1),
    ):
        journal.record_leg_fill(
            fill_id=activity,
            parent_fill_id="broker-activity-short" if linked else None,
            client_order_id="report-execution",
            occ_symbol=symbol,
            contracts=2,
            premium_per_contract=price,
            side=side,
            occurred_at=NOW,
            source=FillSource.REST,
            at=NOW,
            detail=json.dumps(
                {"kind": "alpaca_leg_activity_v1", "broker_order_id": "child-" + side}
            ),
        )


def coverage(journal: Journal) -> dict[str, object]:
    result: dict[str, object] = journal.reports_summary(today=NOW.date())["report"][
        "execution_coverage"
    ]
    return result


def test_derived_parent_is_complete_only_with_confirmed_component_legs(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with Journal(path) as journal:
        opening(journal)
        parent(journal)
        before = coverage(journal)
        assert before["parent_complete"] is True
        assert before["leg_complete"] is False
        assert before["complete"] is False
        assert before["status"] == "incomplete"
        assert before["parent_events_with_incomplete_legs"] == 1
        leg(journal)
        complete = coverage(journal)
        assert complete["status"] == "complete"
        assert complete["derived_parent_events"] == 1
        assert complete["raw_leg_activity_events"] == 2
        assert complete["confirmed_fill_events"] == 1
        assert complete["confirmed_spreads"] == 2
        assert complete["confirmed_leg_events"] == 2
        assert complete["confirmed_leg_contracts"] == 4
        # Duplicate deliveries do not increase either layer's counts.
        parent(journal)
        leg(journal)
        assert coverage(journal) == complete
    with Journal(path) as reopened:
        assert coverage(reopened) == complete


def test_unlinked_raw_activities_are_pending_and_never_parent_fills() -> None:
    with Journal() as journal:
        opening(journal)
        leg(journal, linked=False)
        pending = coverage(journal)
        assert pending["status"] == "pending"
        assert pending["complete"] is False
        assert pending["raw_leg_activity_events"] == 2
        assert pending["unlinked_leg_events"] == 2
        assert pending["confirmed_fill_events"] == 0
        assert pending["confirmed_spreads"] == 0


def test_provenance_is_explicit_and_redacts_credential_shaped_detail() -> None:
    result = _execution_provenance(
        json.dumps(
            {
                "kind": "alpaca_leg_activity_aggregate_v1",
                "activity_ids": ["true-broker-activity", "sk-live-AAAABBBBCCCCDDDDEEEEFFFF"],
                "timestamp_semantics": "latest_component_execution_time",
                "private_unrecognized_field": "never publish this",
            }
        )
    )
    assert result["kind"] == "alpaca_leg_activity_aggregate_v1"
    assert result["timestamp_semantics"] == "latest_component_execution_time"
    assert "true-broker-activity" in str(result["activity_ids"])
    assert "AAAABBBB" not in str(result)
    assert "private_unrecognized_field" not in result
    assert _execution_provenance("ordinary legacy detail") == {"kind": "recorded_event"}


def test_missing_broker_parent_price_prevents_complete_even_with_balanced_events() -> None:
    with Journal() as journal:
        journal.record_intent(
            client_order_id="report-execution",
            cycle_id="missing-price",
            symbol="XLE",
            spreads=2,
            payload={},
            legs=(
                IntentLeg(SHORT_LEG, "sell", 1, "sell_to_open"),
                IntentLeg(LONG_LEG, "buy", 1, "buy_to_open"),
            ),
            at=NOW,
        )
        journal.mark_status("report-execution", OrderStatus.FILLED, spreads_filled=2, at=NOW)
        parent(journal)
        leg(journal)
        report = coverage(journal)
        assert report["unknown_filled_price_orders"] == 1
        assert report["parent_complete"] is False
        assert report["leg_complete"] is True
        assert report["complete"] is False
        assert report["status"] == "incomplete"


def test_http_order_evidence_retains_real_component_ids_and_separate_sync_status(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from underwriter.dashboard import DashboardConfig, create_app

    path = tmp_path / "events.db"
    with Journal(path) as journal:
        opening(journal)
        parent(journal)
        leg(journal)
        journal.record_execution_ingestion_status(
            {
                "status": "conflict",
                "activities": 2,
                "pages": 2,
                "leg_events": 2,
                "derived_groups": 1,
                "orders_reconciled": 1,
                "issues": [{"code": "broker_quantity_mismatch", "count": 1}],
            },
            at=NOW,
        )
    with TestClient(create_app(DashboardConfig(journal_path=path, clock=lambda: NOW))) as client:
        orders = client.get("/api/orders").json()
        fill = orders["orders"][0]["fills"][0]
        assert fill["provenance"]["kind"] == "alpaca_leg_activity_aggregate_v1"
        assert fill["provenance"]["activity_ids"] == [
            "broker-activity-short",
            "broker-activity-long",
        ]
        assert fill["provenance"]["timestamp_semantics"] == "latest_component_execution_time"
        assert fill["spreads"] == 2
        assert len(fill["legs"]) == 2
        assert sum(row["contracts"] for row in fill["legs"]) == 4
        assert all(row["confirmed_at"] and row["recorded_at"] for row in fill["legs"])
        assert orders["execution_ingestion"]["status"] == "conflict"
        reports = client.get("/api/reports").json()
        # Latest account-wide sync status is distinct from selected-order quantities.
        assert reports["report"]["execution_coverage"]["complete"] is True
        assert reports["execution_ingestion"]["status"] == "conflict"
        assert reports["execution_ingestion"]["issues"] == [
            {"code": "broker_quantity_mismatch", "count": 1}
        ]


def test_execution_ingestion_failures_are_operational_in_reports_and_archive(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from underwriter.dashboard import DashboardConfig, create_app
    from underwriter.journal import Stage

    path = tmp_path / "operational.db"
    with Journal(path) as journal:
        for stage, reason in (
            (Stage.MONITOR, "reconcile_error"),
            (Stage.RISK, "execution_evidence_unavailable"),
        ):
            journal.record_decision(
                cycle_id="ingest-failed",
                stage=stage,
                accepted=False,
                reasons=(reason,),
                detail=("Execution evidence could not be established",),
                at=NOW,
            )
        journal.record_decision(
            cycle_id="ordinary-refusal",
            stage=Stage.RANK,
            accepted=False,
            reasons=("premium_below_floor",),
            at=NOW,
        )
        journal.record_decision(
            cycle_id="research",
            stage=Stage.EXPLORE,
            accepted=False,
            reasons=("execution_evidence_unavailable",),
            at=NOW,
        )
    with TestClient(create_app(DashboardConfig(journal_path=path, clock=lambda: NOW))) as client:
        history = client.get("/api/history").json()
        report = client.get("/api/reports").json()["report"]
        assert history["totals"]["operational"] == report["metrics"]["operational"] == 2
        assert report["metrics"]["affected_cycles"] == 1
        assert report["metrics"]["strategy_rejections"] == 1
        assert {row["category"] for row in report["operational"]} == {
            "reconcile_error",
            "execution_evidence_unavailable",
        }
        assert {row["reason"] for row in report["strategy_reasons"]} == {"premium_below_floor"}
        for reason in ("reconcile_error", "execution_evidence_unavailable"):
            archive = client.get(
                "/api/history/decisions",
                params={"track": "live", "outcome": "operational", "reason": reason},
            ).json()
            assert archive["page"]["total"] == 1
            assert archive["decisions"][0]["cycle_id"] == "ingest-failed"
        strategy = client.get(
            "/api/history/decisions", params={"track": "live", "outcome": "strategy"}
        ).json()
        assert strategy["page"]["total"] == 1
        assert strategy["decisions"][0]["cycle_id"] == "ordinary-refusal"
