# Self-hosted Underwriter

Migration date: 2026-09-12 UTC (2026-09-11 America/Chicago).

- Public hostname: `underwriter.ianlan.dev`.
- App: `ianwalmsley@10.80.13.12`, `/opt/underwriter`.
- Caddy: `ianwalmsley@10.80.13.11`, upstream `http://10.80.13.12:18082`.
- DNS: an A record for `underwriter` pointing to `23.155.40.115`.
- Docker Compose runs the dashboard and paper agent together with restart policy
  `unless-stopped`, a health check, and bounded container logs.
- The SQLite journal is bind-mounted from `/opt/underwriter/data/underwriter.db`.
- Production settings are in `/opt/underwriter/.env.production` (mode 600).
  Preserve the paper-only settings and never commit this file.

## Deploy and inspect

Transfer the source files used by the Dockerfile, plus `compose.yaml` and
`.dockerignore`, to `/opt/underwriter`. Keep the existing secrets, data, and backups.
Then run on the app server:

```sh
cd /opt/underwriter
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=50
curl -fsS http://10.80.13.12:18082/api/health
```

Check that startup logs report a clear Alpaca preflight and an initialized agent.
The HTTP health endpoint confirms journal readability; it does not by itself prove
that a market cycle completed. Run exactly one agent against this paper account.

## Caddy

The route is stored both in `/etc/caddy/Caddyfile` and the Caddy portal database
at `/opt/stacks/caddy-portal/data/portal.sqlite`. Manage subsequent route edits in
the portal as well, because a portal apply regenerates the Caddyfile.

```caddyfile
underwriter.ianlan.dev {
    reverse_proxy http://10.80.13.12:18082
}
```

Caddy obtains and renews the HTTPS certificate once public DNS resolves to the
edge. Verify the public dashboard and `/api/health` before retiring an old host.

## Journal backup and recovery

The migration snapshot is `/opt/underwriter/backups/underwriter.db`, with table
counts in `journal-counts.json`. Its SHA256 is
`5c7c50e5d1583e0bbcf65c600e5bbe6f917d3d29b8ef918eb841d77baff25946`.
An additional copy and the original Railway variables are in the local ignored
`artifacts/migration-20260912/` directory. These contain private runtime data.

Use SQLite's online backup API for future live backups; copying only the database
file while it is running can omit committed WAL records. To restore, stop the
container, preserve the entire current `data` directory (including WAL files),
create a fresh `data` directory containing the verified backup as
`underwriter.db`, and start the container. Never restore an older journal over a
running agent or leave stale WAL files beside a restored database.

The original Railway project was `underwriter`, ID
`8d9a4840-d83d-49e0-9f63-4448d0c12cd5`. Its deployment was stopped before starting
the new agent. On 2026-09-12 UTC, DNS and the Let's Encrypt certificate were
verified, and the public dashboard, ledger, and all eight API endpoints returned
HTTP 200. Railway then accepted the project deletion request and reported
`deletedAt: 2026-09-14T00:43:31.728Z` (its deletion grace period).

## Connectivity repair (2026-10-17)

The blank `UNDERWRITER_MODEL_BASE_URL=` setting was passed to the OpenAI SDK as
an explicit empty URL. The SDK wrapped `UnsupportedProtocol` in
`APIConnectionError`, and the agent correctly vetoed every affected candidate.
The journal contained 4,183 such vetoes between September 4 and October 16.
`build_veto` now treats blank or whitespace-only overrides as absent and rejects
nonblank URLs without an HTTP(S) scheme and host during construction.

Separate intermittent Alpaca name-resolution failures were recorded by the app;
the host resolver also logged degraded responses from its LAN DNS forwarder.
Compose now supplies `1.1.1.1` and `8.8.8.8` for this container's public API
lookups. Docker still provides its embedded resolver for the Compose network.

Deployment validation passed: 99 configuration/veto tests, Ruff, and the veto
module's type check; authenticated Alpaca account, clock, and stock-bar requests;
news retrieval; and a real `CatalystVeto.screen` call using a saved GLD volatility
observation. The model returned valid JSON and a non-veto decision. This was an
isolated screening diagnostic, not an order or an override of the ranking/risk
rules. The journal still contained zero orders after the check.

The dashboard health check only proves the web process and journal are readable.
After changing provider settings, also make a harmless model screening request
from inside the container and confirm it parses successfully. An HTTP 200 from
`/api/health` alone does not verify the model, Alpaca, or DNS connectivity.

Rollback files and the pre-repair journal snapshot are in
`/opt/underwriter/backups/connectivity-20261017/`; the previous image is tagged
`underwriter:pre-connectivity-20261017`. A code rollback does not require restoring
the journal. Preserve newer decisions instead of replacing the running journal
with the migration or repair snapshot.

## Complete history dashboard (2026-10-17)

`/history` shows the full journal with date presets and inclusive Eastern-time
date ranges. The initial production run spans August 31 through October 16:
47 calendar days, 35 recorded days, 2,816 cycles, 32,592 decisions, and 6,951
P&L snapshots. There are no recorded orders or fills.

`GET /api/history` aggregates the entire selected range, without the older
dashboard's recent-row cap. Activity charts, rejection reasons, stage counts,
symbol summaries, and session records share a SQLite read snapshot. Charts use
daily, weekly, or monthly buckets as the range grows. Equity uses the last
observation per bucket and source; official, shadow, and exploratory series
remain separate, missing observations remain unknown, and snapshots are never
summed as returns. CSV exports contain the displayed period aggregates.

`GET /api/history/decisions` provides a complete archive through stable keyset
pagination (50 rows by default, 200 maximum per response), with date, track,
symbol, stage, outcome, reason, and reasoning/cycle search filters. A snapshot
watermark prevents new inserts from shifting an archive being paged through.
Chart drill-downs preserve their live-strategy scope; the archive can also show
exploratory records. Historical operational failures are explicitly separated
from ordinary strategy rejections and are not presented as a current outage.

Validation: full Python test suite, targeted date/DST, aggregation, pagination,
and track-filter regression checks, Ruff and type checks; browser chart/archive
checks using a current journal backup; public HTTPS verification of all-time
counts, the first recorded day, failure filtering, health, and overview.
The production all-time summary returned in approximately one second.

Rollback assets are `/opt/underwriter/backups/history-20261017/source-before.tar.gz`
and image `underwriter:pre-history-20261017`. That directory also contains a
consistent pre-deployment journal backup. Roll back code/image independently
of journal data; this deployment makes no database schema changes.

## Dashboard workspace release (2026-10-17)

The public application now has four destinations, with shared navigation:

- `/` — the underwriting desk: observed equity, recorded operating posture,
  book risk, full-run context, interactive activity/equity readouts, and recent
  account detail. Current observations refresh every four seconds; the complete
  history summary refreshes at startup, every minute, or on manual refresh.
- `/history` — the research record: date ranges, session calendar, data-point
  readouts, symbol/session drilldowns, independent source charts, CSV, and the
  complete searchable archive with persistent URL filters.
- `/reports` — research and operations: equal-calendar-period comparisons,
  failure incidence by distinct cycle, strategy reasons, execution evidence,
  reconciliation/regime controls, source observations, coverage, CSV and print.
- `/ledger` — the evidence desk: recent decisions, expandable recorded context,
  reason examples, observed positions and the order tape. Its API adapter reads
  the current response envelopes rather than the older bare-array format.

`GET /api/reports` adds report aggregates to the history response. It reads one
SQLite snapshot and never changes the journal. On the October 17 backup, 4,223
live-strategy operational failure events affected 1,216 distinct recorded
cycles; 26,352 other live records were strategy rejections. A failed stage does
not establish that its whole cycle stopped or that a trade would have occurred.
The report distinguishes primary failure categories from links to related
reason evidence. `outcome=strategy` in the archive excludes operational records
even when they also carry ordinary strategy reason codes.

Dashboard API connectivity is labelled separately from the agent's recorded
state. A closed-market view retains its actual observation age and qualifies
that no current session mark exists. Neither a successful browser poll nor a
historical failure count proves current broker/model connectivity. Complete
provider checks remain the separate procedure above.

The developer workflow and data contracts are in
[`dashboard-development.md`](dashboard-development.md). This release changes
presentation and read-only reporting; the running policy and database schema
are unchanged.

Pre-release verification passed: 1,126 tests, Ruff, strict mypy across 50 Python
source/test files, JavaScript syntax checks for all four pages, real-journal
desktop and phone-width checks, empty-report states, populated order/position
fixture rendering, specimen labels, filtered archive landing and pagination,
the first recorded session, and a downloaded CSV checked against its totals.
The actual-journal reports query took approximately 0.95 seconds locally.

### Releasing a tested revision

Keep production running during development and validation. Package a committed
revision with `git archive`; do not copy an arbitrary working directory, local
credentials, preview journals, or generated submission media into the release.
Transfer the archive from a machine with the authorized SSH key, extract it in
`/opt/underwriter`, then rebuild and recreate the existing Compose service.
The ignored `.env.production` and mounted `data/` directory stay on the server.
Do not start another supervisor during preview or verification.

Before recreation, save the current image under a rollback tag and use SQLite's
online backup API. This release's backup is
`/opt/underwriter/backups/command-center-20261017/`, containing the pre-release
source archive and journal; the image is
`underwriter:pre-command-center-20261017`. The deployed commit is recorded in
`/opt/underwriter/REVISION` and the release archive is retained under `backups/`.

After deployment, verify:

```bash
cd /opt/underwriter
sudo docker compose ps
sudo docker compose logs --tail 40 app
curl --fail https://underwriter.ianlan.dev/api/health
curl --fail https://underwriter.ianlan.dev/api/history
curl --fail https://underwriter.ianlan.dev/api/reports
```

Confirm that one app container is healthy, all four public pages load, report
totals match the journal, a report link opens the correctly filtered archive,
and the first recorded day remains accessible. Keep `restart: unless-stopped`
and the Compose health check in place. Validate provider availability separately
when a provider setting changes; do not place a diagnostic order for a dashboard
release.

To roll back this release's application image while preserving newer records:

```bash
cd /opt/underwriter
sudo docker tag underwriter:pre-command-center-20261017 underwriter:local
sudo docker compose up -d --no-build
sudo docker compose ps
```

The previous image contains the connectivity repairs and original history view.
Restore source from the corresponding archive if developing or rebuilding that
version later. Do not restore the pre-release database for a code rollback.

## Automatic closing reports (2026-10-17)

The existing agent supervisor runs a closing observation at **16:20 Eastern**
(15:20 America/Chicago) on weekdays. The broker calendar skips holidays; an
early-close session still receives its report at 16:20. This is an application
job, so no cron entry, second agent, browser tab or Codex automation is needed.
The job observes positions and account state, reconciles known orders, records
official and shadow P&L, then saves session-scoped History and Reports JSON.
It never invokes candidate scans, model screening, entries or exits, and it
never invents an opening baseline or adds evaluation decisions.

Files live beside the journal on the persistent bind mount:

```text
/opt/underwriter/data/daily-reports/status.json
/opt/underwriter/data/daily-reports/YYYY-MM-DD/history.json
/opt/underwriter/data/daily-reports/YYYY-MM-DD/reports.json
/opt/underwriter/data/daily-reports/YYYY-MM-DD/status.json
```

The two artifacts are atomically replaced before their session completion
marker. A restart after a partial write retries the session. Failed broker
reads, missing marks and write failures retry 15 minutes after the failure;
they do not publish a successful close. A restart after 16:20 catches up that
same day. It deliberately does not backfill yesterday using today's broker
readings. Preserve this directory with the journal during future migrations.
There is no automatic report retention or deletion.

`GET /api/end-of-day` exposes safe schedule and completion metadata. History
and Reports check it once a minute while visible and refresh their aggregates
after a new completion. Existing dates, filters and chart sources remain
selected; an archive being paged stays pinned to its insertion watermark until
manual refresh. The Overview continues its four-second current-data polling.
Confirmed orders and fills appear when the normal agent records them; the
closing job provides another observation after trading, not a live fill feed.

The initial deployment is on Saturday, October 17. Its first scheduled attempt
is Monday, October 19 at 16:20 Eastern (20:20 UTC), subject to the broker's
calendar. No historical completion is fabricated during deployment. Check:

```bash
curl --fail https://underwriter.ianlan.dev/api/end-of-day
cd /opt/underwriter
sudo docker compose logs --tail 60 app
sudo find data/daily-reports -maxdepth 2 -type f
```

Before the first run, the directory may not exist and status should be
`scheduled`. After a successful run, verify `status: complete`, matching
`session_date` and `completed_session_date`, fresh `observed_at` and
`completed_at`, both JSON artifacts, and a completed receipt on History and
Reports. A holiday is `skipped`. On `failed`, use `error_type`, `retry_at` and
the private service logs to diagnose the provider or filesystem; the public
API intentionally omits exception messages and server paths. A healthy HTTP
endpoint alone does not establish that the close job completed.

Verification passed: 1,160 Python tests, Ruff, strict mypy across 53 Python
source/test files, and both JavaScript completion-race regression checks.
Scheduler tests include restart recovery, partial files, pointer publication
failure across midnight, DST and retry timing. Broker observation tests prove
that the job cannot submit orders or create a session-opening baseline.

Rollback assets are in `/opt/underwriter/backups/end-of-day-20261017/`; the
previous image is `underwriter:pre-end-of-day-20261017`. To roll back code,
tag that image as `underwriter:local` and run `sudo docker compose up -d
--no-build`. Preserve the journal and closing artifacts. This release makes
no database schema changes; restoring the pre-release database is unnecessary.
