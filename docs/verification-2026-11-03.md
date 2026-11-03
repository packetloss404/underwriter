# Feature verification — November 3, 2026

**Historical initial audit:** this records the release before execution ingestion.
The later [execution-ingestion release](execution-ingestion.md) addresses the missing
event pipeline and reconciles its historical broker records. The observations below
retain their original timestamps and limitations.

This is a dated engineering audit of the active self-hosted application. Tests
exercise real application code and HTTP/browser flows with isolated journals and
injected broker adapters. They do not establish strategy profitability, real-market
liquidity, or a successful live order lifecycle during this release.

## Reproduce the checks

Use Python 3.12, uv, and Node.js 22 or later:

```bash
uv sync --locked --all-groups
uv run pytest
uv run ruff check .
uv run mypy src tests
npm ci
npx playwright install chromium
npm run test:closeout
npm run test:browser
npm run test:smoke
```

On Linux, Playwright may require `npx playwright install --with-deps chromium`.
With Windows Node in a WSL UNC checkout, invoke the CLI directly because npm's CMD
script shell cannot preserve that working directory:

```powershell
node node_modules/@playwright/test/cli.js test
node node_modules/@playwright/test/cli.js test --config=playwright.smoke.config.cjs
node tests/check_closeout_ui.cjs
```
The browser launcher supports native uv and Windows Node over a WSL UNC checkout.
It creates temporary populated and empty SQLite journals, serves the real FastAPI
application without the trading supervisor, and removes the journals on shutdown.
It never uses production credentials. The public smoke test uses GET requests and
browser navigation at `https://underwriter.ianlan.dev`; override
`UNDERWRITER_SMOKE_URL` to inspect another deployed instance.

GitHub Actions runs the Python, lint, type, closeout-race, and fixture-browser checks
on pushes and pull requests. It does not deploy or run the public smoke test.

## Advertised behavior and evidence

- **Scan, rank, regime, veto, risk, execute, monitor, exit, review:** existing
  `test_cycle.py` coverage plus `test_strategy_lifecycle.py` exercise deterministic
  market/account inputs, write-ahead intents, ambiguous submissions, reconciliation,
  reopened journal connections, exits before entries, and failure gates. These are
  injected integrations, not additional orders against the paper account.
- **Paper-only and risk controls:** `test_config.py`, `test_preflight.py`,
  `test_risk.py`, and `test_exits.py` cover the live-host prohibition, permissions,
  missing/invalid numeric inputs, caps, baseline-dependent loss stops, broker clock,
  kill switch, and exit deadlines. The deployed builder requires the veto in effect;
  missing model configuration cannot silently authorize unscreened entries.
- **Market-data semantics:** `test_data.py` captures actual SDK request objects,
  including Eastern-date/DST bounds. Today's partial daily bar is excluded;
  ranking uses 10-session realized volatility and 20-session expansion context.
- **Persistence and reports:** `test_journal.py`, `test_restart_recovery.py`,
  `test_endofday.py`, and `test_close_observation.py` cover durable recovery,
  closing artifacts, holiday/retry behavior, partial publication, and observations
  that cannot place orders. An incomplete shadow observation cannot finalize a
  successful closing report.
- **Dashboard/API:** `test_dashboard.py` and `test_history.py` cover GET-only
  routes, credential-shaped free-text redaction, full-range counts, independent
  source labels, Eastern-time/DST dates, operational/strategy scope, comparisons,
  archive watermarks, and pagination. Non-finite JSON values are rejected at the
  journal write boundary; invalid legacy contexts remain unknown in aggregates.
- **Real browser flows:** `tests/browser/dashboard.spec.cjs` opens all four pages
  in desktop and phone Chromium. It exercises navigation, chart readouts and
  source selection, presets/custom dates, full archive pagination, URL reload,
  filters/search/context, report drilldowns, CSV download contents, print invocation,
  labelled specimens, empty/error/recovery states, and responsive document bounds.
  `check_closeout_ui.cjs` is a separate JavaScript completion-race test, not a browser.
- **Static export:** `test_export.py` and a real browser test verify a self-contained,
  labelled finite snapshot and working evidence/ledger links. It is deliberately
  different from the live four-page research application: no dynamic reports,
  arbitrary date/filter queries, automatic refresh, or complete decision pagination.
- **Process supervision and packaging:** `test_serve.py` verifies dashboard startup
  or runtime failure stops the supervisor and returns nonzero, and agent construction
  failure stops the web thread. Installed entry points were imported successfully;
  `underwriter-export --help` ran. The nonexistent generic `underwriter` entry point
  was removed; `underwriter-serve`, `calibrate`, and `underwriter-export` remain.

## Failures repaired

The initial Python run reproduced **1,159 passes and one failure**. The failure used
the real November clock while asserting an October retry deadline; its clock is now
injected. Additional audit regressions exposed and fixed:

- Missing model configuration bypassing the production veto.
- Closed broker clocks permitting entries during holidays/pre-open/early closes.
- NaN/infinity bypassing numeric risk/preflight checks.
- Incomplete same-day bars entering realized-volatility calculations.
- A dead dashboard leaving the agent alive.
- Provider/error sentences escaping public redaction.
- NaN/Infinity in journal JSON breaking SQLite historical queries.
- Static exports requesting unexported APIs/assets and linking to absent pages.
- Shadow costs treating $0.05 as $5, and open premium being counted as realized
  before adding open-position P&L again.
- Shadow completeness ignoring filled parent orders without execution-event evidence.

## Live observations before release

At approximately 14:23 America/Chicago (20:23 UTC) on November 3, public all-time
aggregates reported 40,225 decision records, 3,783 cycles, 47 recorded dates,
28 parent orders, and zero rows in the separate spread-fill event table.
These values are timestamped observations and change as the agent runs.

A read-only production SQLite check returned `quick_check: ok`. Parent orders
comprised **26 `filled` statuses and two `expired` statuses**. Zero fill-event rows
must not be described as zero filled orders or zero actual executions. Reports now
show execution-evidence coverage and warn about the difference.

The paper account was active with effective options level 3 and zero current option
positions during the diagnostic. Authenticated account, clock, position, historical
bar, and GLD option-chain reads succeeded (1,092 contracts in the sampled 5–14 DTE
window). News returned three headlines. The configured OpenAI adapter returned a
valid boolean veto JSON response using observed GLD volatility and the real news.
This was a screening diagnostic, not a candidate approval or an order. Other model
vendors are covered by adapter/configuration tests, not live credential checks.

Eleven session completion markers from October 19 through November 2 had both JSON
artifacts present. This proves persistence/publication occurred; it does **not**
validate their historical shadow calculations. November 2 completed at 21:20 UTC,
reflecting the return to Eastern Standard Time.

## Remaining limits

The production path reconciles cumulative parent-order status, quantity, and average
price. **No automatic execution-event or trade-update stream ingestor is wired.**
Parent-order observations do not provide a full event-by-event execution history.
The release does not fabricate activity identifiers, infer execution timestamps, or
rewrite historical shadow rows. New shadow snapshots are withheld when confirmed
execution-event coverage is incomplete; the closing job consequently reports failure
and retries rather than claiming a complete paired report. Historical shadow snapshots
remain recorded observations and are not independently validated results.

The current shadow calculation is a broker-derived repricing adjustment, retaining
the official open marks. The field called realized P&L is a daily residual
(`equity - previous-close equity - current open-position P&L`), not an
independently observed closed-trade profit. Indicative quotes and paper simulation still limit it.
Orders marked filled, recorded event rows, stage approvals, spread units, and trades
are different quantities. No win rate, fill quality, or profitability conclusion
follows from this audit.

The time stop requests exits at two calendar days to expiry. An expiration-day
fallback uses a fixed 15:15 Eastern cutoff and does not dynamically adjust for early
closes. It cannot guarantee a fill. Automated rolling and the strategy document's
baseline backtest comparisons are not implemented. No live submit/cancel, assignment,
expiration, model-provider failover, or outage was deliberately induced in production.
Those failure paths use injected test adapters.

The historical submission package retains its dated contest claims and results;
use this audit and the current runbook for operational decisions.


## Final local Python checks

The integrated release candidate passed **1,216 Python tests**, Ruff, strict mypy
across **56 source/test files**, and `git diff --check`. The suite emitted one
upstream `websockets.legacy` deprecation warning. Both History/Reports completion-race
checks also passed. The installed console targets imported successfully, and the
static export's help command ran without provider access.


The final actual Chromium run passed **44 cases in 1.9 minutes**: 22 desktop and
22 phone cases, including additional 320-pixel checks. It covered real HTTP-backed
aggregates, downloads, print invocation, all four destinations, archive pagination,
source/date/filter scope, explicit specimen replay disclosures, outage recovery,
finite exports, and automatic closeout refresh retaining the pinned second archive
page. The targeted replay/closeout subset also passed before the full run.


The first hosted CI attempt stopped before checkout because the initial `setup-uv`
major tag did not exist. The workflow now uses verified immutable action revisions,
including the [upstream documented setup-uv release](https://github.com/astral-sh/setup-uv#usage),
and Ubuntu 24.04, which is supported by the pinned Playwright dependency manifest.
Hosted CI is a release gate; a runner setup failure is not recorded as a test pass.


The clean runner then exposed an existing test prerequisite: CLI transport and
preflight checks require the pinned Alpaca CLI to be on PATH, even though their
order responses are injected. CI installs v0.0.14 exactly as the Docker image/setup
guide does and executes `alpaca version` before the regression suite. It supplies
no broker credentials and cannot submit a real diagnostic order.
