# Dashboard development

The application has four read-only destinations:

- `/`: account overview, recorded operating posture, book, recent decisions,
  and context across the full run.
- `/history`: time-series exploration, session and symbol summaries, and the
  complete paginated decision archive.
- `/reports`: strategy and operational reports, execution and reconciliation
  evidence, coverage, and comparisons with the preceding period.
- `/ledger`: recent decisions and order evidence, including the labelled demo.

All four use the existing navy ground, brass refusal accents, cyan analytical
marks, Instrument Serif headings, Public Sans text, and IBM Plex Mono figures.
Keep money and status visible early; explanations and raw context belong beside
the evidence they qualify.

## Local preview without running a second agent

Use Python 3.12 and install dependencies with `uv sync --all-groups`. Preview a
consistent copy of the journal through the dashboard app alone. Never start
`underwriter-serve` against production credentials merely to inspect a page:
that entry point also starts the trading supervisor.

From the repository root, after creating a private ignored `artifacts/preview/`
directory and placing an online SQLite backup at `artifacts/preview/underwriter.db`:

```bash
uv run python - <<'PY'
from pathlib import Path
import uvicorn
from underwriter.dashboard import DashboardConfig, create_app

app = create_app(DashboardConfig(
    journal_path=Path("artifacts/preview/underwriter.db"),
))
uvicorn.run(app, host="127.0.0.1", port=18083)
PY
```

Open `http://127.0.0.1:18083/`. An empty local journal is also useful for
validating missing-data states. Stop this preview when finished. In WSL, a
preview intended for the Windows browser may need to bind `0.0.0.0`; keep it
temporary and use the existing local network policy.

## Implementation boundaries

`src/underwriter/dashboard.py` owns GET routes, parameter validation, response
envelopes, and credential redaction. `JournalGateway` serializes reads on its
SQLite-owning thread. `src/underwriter/history.py` performs historical SQL
aggregation against a coherent read snapshot. The live agent retains its own
writer connection in WAL mode. Dashboard changes must never place orders,
write journal records, alter risk settings, or change the kill switch.

The pages are buildless HTML with inline CSS and JavaScript under
`src/underwriter/static/`. The `/static/{name}` endpoint has an explicit image
allowlist; adding a standalone script or stylesheet requires updating serving
and security behavior deliberately. No chart dependency or frontend build is
required.

`src/underwriter/endofday.py` runs a persistent closing job through the
supervisor's existing journal-owning thread. It is due at 16:20 Eastern on
weekdays; the callback checks the broker calendar and skips holidays. The
dedicated `Cycle.observe_after_close` path observes positions, reconciles known
orders and records P&L without invoking scans, model screening, entries or exits.
It does not write a session-opening baseline or add evaluation decisions.
The job publishes artifacts only when the observations and P&L sources succeed.

The broker and market-data SDK reader sessions have five-second connect and
twenty-second read timeouts; CLI reconciliation already has its own timeout.
Keep report work bounded because it shares the supervisor thread. The separate
SDK order-submission client is not changed by the reader timeout hook.

## Data contracts

Every live API response includes `generated_at`, `data_as_of`, and
`data_age_seconds`. Page fetch success establishes dashboard API reachability;
it does not establish broker, model, DNS, or agent health. Market schedules
qualify whether new observations are expected. Keep the actual last observation
timestamp visible even while entries are outside their scheduled window.

`GET /api/overview` is the account and operating snapshot. The other recent
endpoints return envelopes containing their named collections rather than bare
arrays: decisions, rejections, positions, orders, or P&L series. Preserve the
existing response structure and inspect it before adapting a renderer.

`GET /api/history?start=YYYY-MM-DD&end=YYYY-MM-DD` returns:

- Global coverage and inventory, plus the actual selected date range.
- Exact selected-period totals, stages, refusal reasons, and live-strategy symbols.
- Calendar buckets containing activity, orders/fills, regime and reconciliation
  evidence, and observed position counts.
- Separate last-observed P&L records per bucket/source and first/last equity
  observations for each source.

Date bounds are inclusive in `America/New_York`, converted to half-open UTC
query bounds with DST accounted for. SQL aggregates all selected records;
chart resolution changes from daily to weekly or monthly for long ranges.
Reducing chart points must never silently cap the underlying totals.

`GET /api/history/decisions` accepts dates, `track=live|exploratory`, `symbol`,
`stage`, `reason`, `outcome=accepted|rejected|operational|strategy`, and reasoning/cycle
search `q`. Pages default to 50 records, with at most 200 per response. Carry
the response's `snapshot` watermark and `next_cursor` as `before` for the next
page. `strategy` selects rejected live-strategy records with no operational
failure marker, including when a record carries several reasons. Reset
pagination when filters change. Sort order is timestamp then ID,
both descending, so equal timestamps and new inserts cannot shift older pages.

`GET /api/reports` uses the same date conventions and complete selected-period
records. Comparison periods have the same calendar length immediately before
the selection. Coverage and observation counts must remain visible when a
comparison lacks data; a missing baseline is not a zero-percent result.

The report adds `report.metrics`, `comparison`, `strategy_reasons`,
`operational`, `cycle_timeline`, `reconciliation`, `regime_blocks`,
`order_statuses`, `fill_sources`, `coverage`, `pnl_sources`, and explanatory
`notes`. Operational categories assign one primary category per failed record;
cycle incidence counts distinct IDs rather than summing affected stage events.

`GET /api/end-of-day` reads the closing job's status without starting it. It
returns `enabled`, `status` (`scheduled`, `running`, `complete`, `failed`, or
`skipped`), exchange `timezone`, `schedule`, `next_due`, `session_date`,
`completed_session_date`, `completed_at`, `observed_at`, `last_attempt_at`,
`retry_at`, `error_type` and `skip_reason`. Unconfigured previews return
`enabled: false`, `status: disabled`. Public metadata excludes raw exception
messages and filesystem paths.

History and Reports check this metadata every minute while visible and on tab
resume. A newly completed timestamp refreshes their aggregates while preserving
the selected dates, filters and chart source; an active paginated archive retains
its insertion watermark. Editing defers automatic rendering. The Overview's
four-second current-data polling and minute-level historical context stay intact.

For a local EOD receipt preview, pass `end_of_day_dir=Path("artifacts/preview/daily-reports")`
in `DashboardConfig`. Serving the status does not create files or run the job.

## Deep links

History accepts archive filters directly in the URL. Reports, chart points,
and symbol readouts should link to the evidence with matching scope:

```text
/history?start=2026-09-01&end=2026-09-30&track=live&outcome=operational#archive
/history?symbol=SPY&stage=veto&reason=model_unavailable&track=live#archive
```

The optional `q` search is URL encoded. `metric=cycles` and `source=shadow`
select chart state. A live-strategy refusal count must drill into live-strategy
rejections rather than silently including exploratory observations. Preserve
filters while changing pages, and make reset return to the full selected range.

## Analytical rules

- Count decisions as stage events. They are not unique opportunities, orders,
  fills, or trades. A cycle may have many decisions and reason codes.
- Keep official, shadow, and exploratory sources separate. Never sum or blend
  snapshots, equity, or P&L across sources. Account equity changes can include
  adjustments and are not a measured strategy return.
- Take the last observation per chart bucket for snapshot values. Do not sum
  repeated realized P&L snapshots as daily profit.
- Distinguish a measured zero from no observation. Break equity lines across
  missing buckets and show unknown book counts as unknown.
- Operational failures may block opportunities. Label historical failure counts
  as historical; they do not prove a current outage or a working provider.
- Empty execution history cannot support win rate, profit factor, or fill-quality
  claims. Render an informative empty state rather than invented performance.
- Volatility ratios summarize recorded observations. They are not a continuous
  market-price series or a representative sample of every market minute.

## Acceptance checks

Run `uv run pytest`, `uv run ruff check .`, and `uv run mypy src tests` before
release. The history tests cover totals beyond 1,000 rows, Eastern-time/DST
boundaries, independent P&L sources, calendar gaps, stable pagination with new
inserts, and track-scoped failure drilldowns. Route tests enforce GET-only
methods and credential redaction.

Run `node tests/check_closeout_ui.cjs` for History and Reports. It executes the
actual inline request functions with deferred responses to cover a close job
finishing during initial load, unchanged completion markers, failed requests,
editing, hidden tabs and the pinned archive. The Python close-job tests cover
DST, holidays, retries measured from failure, restart/partial-write recovery,
date boundaries and the observation path's prohibition on order submission.

In a browser, check all four pages against a recent journal backup, including:

1. The first screen at desktop and narrow mobile widths; navigation and key
   figures must fit, with horizontal scrolling confined to wide tables.
2. Date presets, custom dates, the first recorded day, empty periods, refresh,
   source selection, and chart/heatmap data-point readouts.
3. A report-to-archive link, symbol/reason filters, search, expanded context,
   newer/older pages, URL reload, and CSV export.
4. Weekend/closed-market status, stale/missing observations, fetch failures,
   and an empty journal. Polling success must not be presented as provider health.
5. `?demo` on overview and ledger. Specimen records must remain clearly labelled
   and must never be mixed into real history or reports.

The legacy static exporter supports a limited snapshot workflow. It does not
provide the dynamic queries, comparison reports, or complete paginated archive
of the running service; use the live FastAPI preview for this acceptance pass.

Release from a tested commit, preserve rollback code/image and an online SQLite
backup, and follow [the deployment runbook](deployment-self-hosted.md).
