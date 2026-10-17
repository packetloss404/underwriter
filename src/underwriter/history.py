"""Read-only, complete-history aggregates and a bounded decision archive.

SQL aggregates the full selection rather than sampling the newest N records.
Only chart resolution is reduced for long runs. Detail rows use stable keyset
pagination. Every query in a response shares one SQLite read snapshot.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from underwriter.journal import EXCHANGE_TZ, DecisionRecord, _to_decision

# Interpolated SQL identifiers and expressions below are fixed in this module;
# all caller-supplied values are bound parameters.
# ruff: noqa: S608

TABLE_TIMES = {
    "decisions": "at",
    "orders": "intent_at",
    "spread_fills": "occurred_at",
    "position_snapshots": "taken_at",
    "reconciliations": "at",
    "regime_verdicts": "at",
    "pnl_snapshots": "at",
    "exploratory_positions": "opened_at",
}
OPERATIONAL = """(
    instr(detail, 'Model unavailable') > 0 OR
    EXISTS (SELECT 1 FROM json_each(decisions.reasons) WHERE value IN
      ('scan_error','preflight_missing','veto_unavailable','backend_unavailable'))
)"""


def _day(value: str) -> date:
    return datetime.fromisoformat(value).astimezone(EXCHANGE_TZ).date()


def _bounds(start: date, end: date) -> tuple[str, str]:
    if end < start or end == date.max or (end - start).days > 36_525:
        raise ValueError("Choose an ordered date range of at most 100 years")
    return (
        datetime.combine(start, time.min, EXCHANGE_TZ).astimezone(UTC).isoformat(),
        datetime.combine(end + timedelta(days=1), time.min, EXCHANGE_TZ)
        .astimezone(UTC)
        .isoformat(),
    )


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def _bucket(day: date, resolution: str) -> date:
    if resolution == "week":
        return day - timedelta(days=day.weekday())
    if resolution == "month":
        return day.replace(day=1)
    return day


def summary(
    conn: sqlite3.Connection, *, start: date | None, end: date | None, today: date
) -> dict[str, Any]:
    """Complete aggregates; at most 366 daily or ~1,200 monthly chart points."""
    conn.execute("BEGIN")
    try:
        return _summary(conn, start=start, end=end, today=today)
    finally:
        conn.rollback()  # End the read snapshot, never mutate the journal.


def _summary(
    conn: sqlite3.Connection, *, start: date | None, end: date | None, today: date
) -> dict[str, Any]:
    inventory = {}
    firsts, lasts = [], []
    for table, stamp in TABLE_TIMES.items():
        row = _rows(
            conn,
            f"SELECT count(*) AS count, min({stamp}) AS first_at, "
            f"max({stamp}) AS last_at FROM {table}",
        )[0]
        inventory[table] = row["count"]
        if row["first_at"]:
            firsts.append(row["first_at"])
            lasts.append(row["last_at"])
    first_at, last_at = min(firsts, default=None), max(lasts, default=None)
    start = start or (_day(first_at) if first_at else today)
    end = end or (_day(last_at) if last_at else today)
    bounds = _bounds(start, end)
    span = (end - start).days + 1
    resolution = "day" if span <= 366 else "week" if span <= 1461 else "month"
    conn.create_function("history_day", 1, lambda at: _day(at).isoformat(), deterministic=True)
    conn.create_function(
        "history_bucket",
        1,
        lambda at: _bucket(_day(at), resolution).isoformat(),
        deterministic=True,
    )
    daily: dict[str, dict[str, Any]] = {}
    cursor = _bucket(start, resolution)
    while cursor <= end:
        daily[cursor.isoformat()] = {
            "date": cursor.isoformat(),
            "decisions": 0,
            "live_decisions": 0,
            "accepted": 0,
            "rejected": 0,
            "operational": 0,
            "exploratory": 0,
            "cycles": 0,
            "orders": 0,
            "fills": 0,
            "reconciliations": 0,
            "reconciliation_failures": 0,
            "regime_allowed": 0,
            "regime_blocked": 0,
            "position_snapshots": 0,
            "max_open_positions": None,
        }
        step = (
            calendar.monthrange(cursor.year, cursor.month)[1]
            if resolution == "month"
            else 7
            if resolution == "week"
            else 1
        )
        if (end - cursor).days < step:
            break
        cursor += timedelta(days=step)
    counts = _rows(
        conn,
        f"""SELECT count(*) AS decisions,
        count(DISTINCT cycle_id) AS cycles,
        count(DISTINCT history_day(at)) AS recorded_days,
        coalesce(sum(stage = 'explore'),0) AS exploratory_decisions,
        coalesce(sum(stage != 'explore'),0) AS live_decisions,
        coalesce(sum(accepted = 1 AND stage != 'explore'),0) AS accepted,
        coalesce(sum(accepted = 0 AND stage != 'explore'),0) AS rejected,
        coalesce(sum(accepted = 0 AND stage != 'explore' AND {OPERATIONAL}),0) AS operational
        FROM decisions WHERE at >= ? AND at < ?""",
        bounds,
    )[0]
    stages = _rows(
        conn,
        f"""SELECT stage, count(*) AS count, sum(accepted) AS accepted,
        sum(accepted = 0) AS rejected, sum(accepted = 0 AND {OPERATIONAL}) AS operational
        FROM decisions WHERE at >= ? AND at < ? GROUP BY stage ORDER BY count DESC""",
        bounds,
    )
    for row in _rows(
        conn,
        f"""SELECT history_bucket(at) AS date, count(*) AS decisions,
        count(DISTINCT cycle_id) AS cycles, sum(stage != 'explore') AS live_decisions,
        sum(accepted = 1 AND stage != 'explore') AS accepted,
        sum(accepted = 0 AND stage != 'explore') AS rejected,
        sum(accepted = 0 AND stage != 'explore' AND {OPERATIONAL}) AS operational,
        sum(stage = 'explore') AS exploratory
        FROM decisions WHERE at >= ? AND at < ? GROUP BY date ORDER BY date""",
        bounds,
    ):
        daily[row["date"]].update(row)
    reasons = _rows(
        conn,
        """SELECT CAST(j.value AS TEXT) AS reason,
        CASE WHEN instr(d.detail,'Model unavailable') > 0 THEN 'model_unavailable'
             ELSE CAST(j.value AS TEXT) END AS category,
        count(*) AS count, min(d.at) AS first_at, max(d.at) AS last_at
        FROM decisions d, json_each(d.reasons) j
        WHERE d.at >= ? AND d.at < ? AND d.accepted=0 AND d.stage != 'explore'
        GROUP BY reason, category ORDER BY count DESC""",
        bounds,
    )
    symbols = _rows(
        conn,
        f"""SELECT symbol, count(*) AS decisions,
        sum(accepted) AS accepted, sum(accepted=0) AS rejected,
        sum(accepted=0 AND {OPERATIONAL}) AS operational,
        count(DISTINCT history_day(at)) AS days, min(at) AS first_at, max(at) AS last_at,
        avg(CASE WHEN json_type(context,'$.vrp_ratio') IN ('real','integer')
            THEN json_extract(context,'$.vrp_ratio') END) AS mean_observed_vrp
        FROM decisions WHERE at >= ? AND at < ? AND symbol IS NOT NULL AND stage != 'explore'
        GROUP BY symbol ORDER BY decisions DESC""",
        bounds,
    )
    for table, stamp in TABLE_TIMES.items():
        if table == "decisions":
            continue
        counts[table] = _rows(
            conn, f"SELECT count(*) AS n FROM {table} WHERE {stamp} >= ? AND {stamp} < ?", bounds
        )[0]["n"]
    for table, stamp, key in [
        ("orders", "intent_at", "orders"),
        ("spread_fills", "occurred_at", "fills"),
        ("position_snapshots", "taken_at", "position_snapshots"),
    ]:
        for row in _rows(
            conn,
            f"SELECT history_bucket({stamp}) AS date, count(*) AS n "
            f"FROM {table} WHERE {stamp} >= ? AND {stamp} < ? GROUP BY date",
            bounds,
        ):
            daily[row["date"]][key] = row["n"]
    for row in _rows(
        conn,
        """SELECT history_bucket(at) AS date, count(*) AS reconciliations,
        sum(ok=0) AS reconciliation_failures FROM reconciliations
        WHERE at >= ? AND at < ? GROUP BY date""",
        bounds,
    ):
        daily[row["date"]].update(row)
    for row in _rows(
        conn,
        """SELECT history_bucket(at) AS date,
        sum(allowed=1) AS regime_allowed, sum(allowed=0) AS regime_blocked
        FROM regime_verdicts WHERE at >= ? AND at < ? GROUP BY date""",
        bounds,
    ):
        daily[row["date"]].update(row)
    for row in _rows(
        conn,
        """SELECT history_bucket(taken_at) AS date, max(n) AS n FROM
        (SELECT p.id,p.taken_at,count(s.symbol) AS n FROM position_snapshots p
         LEFT JOIN snapshot_positions s ON s.snapshot_id=p.id
         WHERE taken_at >= ? AND taken_at < ? GROUP BY p.id)
         GROUP BY date""",
        bounds,
    ):
        daily[row["date"]]["max_open_positions"] = row["n"]
    # Last observation per bucket and series; snapshots are never summed.
    equity = _rows(
        conn,
        """SELECT date,at,source,equity,realised_pnl,unrealised_pnl FROM
        (SELECT history_bucket(at) AS date,at,source,equity,realised_pnl,unrealised_pnl,
         row_number() OVER (PARTITION BY history_bucket(at),source ORDER BY at DESC,id DESC) AS rn
         FROM pnl_snapshots WHERE at >= ? AND at < ?) WHERE rn=1 ORDER BY date,source""",
        bounds,
    )
    equity_endpoints = {}
    for source in ("official", "shadow", "exploratory"):
        endpoints = []
        for order in ("ASC", "DESC"):
            rows = _rows(
                conn,
                f"SELECT at,equity FROM pnl_snapshots WHERE source=? "
                f"AND at >= ? AND at < ? AND equity IS NOT NULL "
                f"ORDER BY at {order},id {order} LIMIT 1",
                (source, *bounds),
            )
            endpoints.append(rows[0] if rows else None)
        equity_endpoints[source] = {"first": endpoints[0], "last": endpoints[1]}
    return {
        "coverage": {"first_at": first_at, "last_at": last_at, "inventory": inventory},
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar_days": span,
            "timezone": "America/New_York",
            "resolution": resolution,
            "complete": True,
        },
        "totals": counts,
        "daily": list(daily.values()),
        "stages": stages,
        "reasons": reasons,
        "symbols": symbols,
        "equity": equity,
        "equity_endpoints": equity_endpoints,
    }


def reports(
    conn: sqlite3.Connection, *, start: date | None, end: date | None, today: date
) -> dict[str, Any]:
    """Historical operations and strategy reports, all from one read snapshot.

    The comparison is an equal number of calendar days immediately before the
    selected period. Missing prior observations do not become a zero baseline.
    Operational incidence is measured in distinct decision cycles, separately
    from stage events and reason mentions. No field implies broker uptime.
    """
    conn.execute("BEGIN")
    try:
        body = _summary(conn, start=start, end=end, today=today)
        selected_start = date.fromisoformat(body["range"]["start"])
        selected_end = date.fromisoformat(body["range"]["end"])
        bounds = _bounds(selected_start, selected_end)

        def period_metrics(window: tuple[str, str]) -> dict[str, Any]:
            metrics = _rows(
                conn,
                f"""SELECT count(*) AS decisions, count(DISTINCT cycle_id) AS cycles,
                count(DISTINCT history_day(at)) AS recorded_days,
                sum(stage != 'explore') AS live_decisions,
                coalesce(sum(accepted=1 AND stage != 'explore'),0) AS accepted,
                coalesce(sum(accepted=0 AND stage != 'explore' AND {OPERATIONAL}),0)
                  AS operational,
                coalesce(sum(accepted=0 AND stage != 'explore' AND NOT {OPERATIONAL}),0)
                  AS strategy_rejections,
                count(DISTINCT CASE WHEN stage != 'explore' AND accepted=0
                  AND {OPERATIONAL} THEN cycle_id END) AS affected_cycles,
                count(DISTINCT CASE WHEN stage != 'explore' AND accepted=0
                  AND {OPERATIONAL} THEN history_day(at) END) AS affected_days,
                max(CASE WHEN stage != 'explore' AND accepted=0 AND {OPERATIONAL}
                  THEN at END) AS last_operational_at
                FROM decisions WHERE at >= ? AND at < ?""",
                window,
            )[0]
            for table, stamp, key in (
                ("orders", "intent_at", "orders"),
                ("spread_fills", "occurred_at", "fills"),
            ):
                metrics[key] = _rows(
                    conn,
                    f"SELECT count(*) AS n FROM {table} WHERE {stamp} >= ? AND {stamp} < ?",
                    window,
                )[0]["n"]
            metrics["live_decisions"] = metrics["live_decisions"] or 0
            metrics["affected_cycle_pct"] = (
                metrics["affected_cycles"] / metrics["cycles"] * 100 if metrics["cycles"] else None
            )
            return metrics

        current = period_metrics(bounds)
        span = body["range"]["calendar_days"]
        comparison: dict[str, Any] = {"available": False, "range": None, "metrics": None}
        if (selected_start - date.min).days >= span:
            prior_end = selected_start - timedelta(days=1)
            prior_start = selected_start - timedelta(days=span)
            previous = period_metrics(_bounds(prior_start, prior_end))
            comparison = {
                "available": bool(previous["decisions"] or previous["orders"] or previous["fills"]),
                "range": {
                    "start": prior_start.isoformat(),
                    "end": prior_end.isoformat(),
                    "calendar_days": span,
                },
                "metrics": previous,
            }
        operational = _rows(
            conn,
            f"""SELECT category, count(*) AS decisions,
            count(DISTINCT cycle_id) AS cycles, count(DISTINCT history_day(at)) AS days,
            min(at) AS first_at, max(at) AS last_at FROM
            (SELECT at,cycle_id,CASE WHEN instr(detail,'Model unavailable') > 0
              THEN 'model_unavailable' ELSE coalesce((SELECT value FROM json_each(reasons)
              WHERE value IN ('scan_error','preflight_missing','veto_unavailable',
                'backend_unavailable') LIMIT 1),'operational_failure') END AS category
             FROM decisions WHERE at >= ? AND at < ? AND accepted=0
             AND stage != 'explore' AND {OPERATIONAL})
            GROUP BY category ORDER BY decisions DESC,category""",
            bounds,
        )
        strategy_reasons = _rows(
            conn,
            f"""SELECT CAST(j.value AS TEXT) AS reason,
            count(DISTINCT decisions.id) AS count,min(at) AS first_at,max(at) AS last_at
            FROM decisions,json_each(decisions.reasons) j
            WHERE at >= ? AND at < ? AND accepted=0 AND stage != 'explore'
              AND NOT {OPERATIONAL}
            GROUP BY reason ORDER BY count DESC,reason""",
            bounds,
        )
        timeline = _rows(
            conn,
            f"""SELECT history_bucket(at) AS date,
            count(DISTINCT cycle_id) AS cycles,
            count(DISTINCT CASE WHEN stage != 'explore' AND accepted=0 AND {OPERATIONAL}
              THEN cycle_id END) AS affected_cycles
            FROM decisions WHERE at >= ? AND at < ? GROUP BY date ORDER BY date""",
            bounds,
        )
        reconciliation = _rows(
            conn,
            """SELECT scope,count(*) AS count,sum(ok=1) AS succeeded,
            sum(ok=0) AS failed,min(at) AS first_at,max(at) AS last_at
            FROM reconciliations WHERE at >= ? AND at < ? GROUP BY scope ORDER BY scope""",
            bounds,
        )
        regime_blocks = _rows(
            conn,
            """SELECT CAST(j.value AS TEXT) AS reason,count(DISTINCT r.id) AS verdicts
            FROM regime_verdicts r,json_each(r.blocks) j
            WHERE r.at >= ? AND r.at < ? AND r.allowed=0
            GROUP BY reason ORDER BY verdicts DESC,reason""",
            bounds,
        )
        order_statuses = _rows(
            conn,
            """SELECT status,count(*) AS orders,sum(spreads_ordered) AS spreads_ordered,
            sum(spreads_filled) AS spreads_filled FROM orders
            WHERE intent_at >= ? AND intent_at < ? GROUP BY status ORDER BY orders DESC,status""",
            bounds,
        )
        fill_sources = _rows(
            conn,
            """SELECT source,count(*) AS fills,sum(spreads) AS spreads
            FROM spread_fills WHERE occurred_at >= ? AND occurred_at < ?
            GROUP BY source ORDER BY fills DESC,source""",
            bounds,
        )
        coverage = []
        for table, stamp in TABLE_TIMES.items():
            row = _rows(
                conn,
                f"SELECT count(*) AS count,min({stamp}) AS first_at,max({stamp}) AS last_at "
                f"FROM {table} WHERE {stamp} >= ? AND {stamp} < ?",
                bounds,
            )[0]
            coverage.append(
                {"table": table, "all_time": body["coverage"]["inventory"][table], **row}
            )
        pnl_sources = _rows(
            conn,
            """SELECT source,count(*) AS snapshots,count(DISTINCT history_day(at)) AS days,
            count(equity) AS equity_observations,min(at) AS first_at,max(at) AS last_at
            FROM pnl_snapshots WHERE at >= ? AND at < ? GROUP BY source ORDER BY source""",
            bounds,
        )
        for source in pnl_sources:
            source["latest"] = _rows(
                conn,
                """SELECT at,equity,realised_pnl,unrealised_pnl FROM pnl_snapshots
                WHERE at >= ? AND at < ? AND source=? ORDER BY at DESC,id DESC LIMIT 1""",
                (*bounds, source["source"]),
            )[0]
        return {
            **body,
            "report": {
                "metrics": current,
                "comparison": comparison,
                "operational": operational,
                "strategy_reasons": strategy_reasons,
                "cycle_timeline": timeline,
                "reconciliation": reconciliation,
                "regime_blocks": regime_blocks,
                "order_statuses": order_statuses,
                "fill_sources": fill_sources,
                "coverage": coverage,
                "pnl_sources": pnl_sources,
                "notes": {
                    "cycles": (
                        "Distinct cycle IDs in decision records, including exploratory records."
                    ),
                    "operational": (
                        "Live-strategy rejected records with a known operational reason or the "
                        "Model unavailable detail marker. Counts describe history, "
                        "not current uptime."
                    ),
                    "categories": "Each failed record has one primary operational category.",
                    "comparison": (
                        "Equal calendar-day windows. Counts are not adjusted for market holidays "
                        "or gaps in observed data. Missing prior observations are "
                        "not a zero baseline."
                    ),
                    "equity": (
                        "Equity change is an account observation, not strategy return. Official, "
                        "shadow and exploratory series are independent. Snapshots are never summed."
                    ),
                },
            },
        }
    finally:
        conn.rollback()


def decisions(
    conn: sqlite3.Connection,
    *,
    start: date | None = None,
    end: date | None = None,
    symbol: str | None = None,
    stage: str | None = None,
    track: str | None = None,
    reason: str | None = None,
    outcome: str | None = None,
    query: str | None = None,
    before: int | None = None,
    snapshot: int | None = None,
    limit: int = 50,
) -> tuple[tuple[DecisionRecord, ...], dict[str, Any]]:
    """Stable, newest-first pages with an insertion watermark and exact total."""
    if not 1 <= limit <= 200:
        raise ValueError("Page size must be between 1 and 200")
    conn.execute("BEGIN")
    try:
        watermark = (
            snapshot
            if snapshot is not None
            else conn.execute("SELECT coalesce(max(id),0) FROM decisions").fetchone()[0]
        )
        clauses, params = ["id <= ?"], [watermark]
        if track == "live":
            clauses.append("stage != 'explore'")
        elif track == "exploratory":
            clauses.append("stage = 'explore'")
        if start and end:
            _bounds(start, end)
        if start:
            clauses.append("at >= ?")
            params.append(_bounds(start, start)[0])
        if end:
            clauses.append("at < ?")
            params.append(_bounds(end, end)[1])
        for column, value in (("symbol", symbol), ("stage", stage)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if reason == "model_unavailable":
            clauses.append("instr(detail,'Model unavailable') > 0")
        elif reason:
            clauses.append("EXISTS (SELECT 1 FROM json_each(reasons) WHERE value=?)")
            params.append(reason)
        if outcome == "operational":
            clauses += ["accepted=0", OPERATIONAL]
        elif outcome == "strategy":
            clauses += ["accepted=0", "stage != 'explore'", f"NOT {OPERATIONAL}"]
        elif outcome in ("accepted", "rejected"):
            clauses.append("accepted=?")
            params.append(int(outcome == "accepted"))
        if query:
            clauses.append(
                "(instr(lower(detail),lower(?)) > 0 OR instr(lower(cycle_id),lower(?)) > 0)"
            )
            params += [query, query]
        where = " AND ".join(clauses)
        total = conn.execute(f"SELECT count(*) FROM decisions WHERE {where}", params).fetchone()[0]
        if before is not None:
            where += " AND (at,id) < (SELECT at,id FROM decisions WHERE id=?)"
            params.append(before)
        rows = conn.execute(
            f"SELECT * FROM decisions WHERE {where} ORDER BY at DESC,id DESC LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        more = len(rows) > limit
        records = tuple(_to_decision(row) for row in rows[:limit])
        return records, {
            "total": total,
            "snapshot": watermark,
            "next_cursor": records[-1].id if more else None,
            "has_more": more,
        }
    finally:
        conn.rollback()
