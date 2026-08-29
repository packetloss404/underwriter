# Deployment: hosting the dashboard for the judged window

**Status:** research and plan. No code changed, nothing installed.
**Written:** 2026-08-29. **Submission due:** Friday 2026-09-04 10:00 CDT.
**First tradeable session:** Monday 2026-08-31.

---

## Verdict

**Export the dashboard to static files and serve them from Cloudflare Workers
static assets. Do not put the laptop on the judged URL.**

The submitted URL must be up when a judge looks at 02:00 on a Wednesday. The
laptop will not be. Every option that routes judge traffic to the operator's
machine — tunnel, quick tunnel, port forward — fails that test by construction,
and WSL2 makes it fail harder than a normal Linux box would (see
[The WSL2 problem](#the-wsl2-problem)).

The unusual thing about this codebase is that the static export is nearly free.
`src/underwriter/dashboard.py` is already shaped for it, by accident of being
written well: every route is a thin wrapper over a pure
`*_payload(journal, *, now, ...)` function, and `src/underwriter/static/index.html`
fetches exactly seven fixed paths with no query strings. So "render the
dashboard to disk" is a loop over seven function calls, not a rewrite.

| | Primary | Fallback A | Fallback B |
|---|---|---|---|
| **What** | Static snapshot → Workers static assets | Same snapshot → R2 public bucket | Snapshot DB + uvicorn on a Fly.io machine |
| **Up when laptop is off** | Yes | Yes | Yes |
| **Setup** | ~2 h | ~30 min (reuses the exporter) | ~45–60 min |
| **Cost** | $0 | $0 | ~$3.50/mo |
| **Needs a domain** | No (`*.workers.dev`) | No (`*.r2.dev`) | No (`*.fly.dev`) |
| **Needs Node** | Yes (wrangler) | No (pure Python/boto3) | No |

A live Cloudflare Tunnel is still worth running **as a supplement** for the demo
video and for watching the agent during market hours. It is not the submitted
URL.

---

## What the app actually is

Facts from the code that drive everything below.

**The dashboard is separable from its server.** `dashboard.py:447-730` defines
`state_payload`, `positions_payload`, `decisions_payload`, `rejections_payload`,
`pnl_payload`, `orders_payload`, `health_payload` — all pure functions of
`(journal, now)` returning plain dicts. The FastAPI routes at
`dashboard.py:783-841` add nothing but parameter parsing.

**The frontend hits seven bare paths.** `static/index.html:1505-1543`:

```
/api/state  /api/rejections  /api/positions  /api/decisions  /api/orders  /api/pnl  /api/health
```

No query strings are ever constructed. The only use of `location.search`
(`index.html:535`) is the demo-mode flag. So a static host serving eight files —
`index.html` plus those seven as extensionless JSON objects — is
byte-for-byte indistinguishable to the frontend. **Zero frontend changes.**

**It is read-only by construction.** Every route is a `GET`; the module header
says so and the code holds to it. `JournalGateway.run` takes a callable rather
than exposing the journal, so routes can only reach it through queries. Nothing
here places, cancels or modifies an order. That property is what makes putting
it on a public URL safe at all, and it survives the static export trivially —
static files cannot mutate anything.

**Secrets are already excluded.** `config.py:131` keeps secrets out of the
dashboard; `_redact` (`dashboard.py:265`) scrubs context blobs; the
journal-unreadable handler (`dashboard.py:765`) deliberately withholds the
database path because "this app is the part of the system a stranger can reach."
Verify once against the exported JSON anyway — see [the pre-flight](#pre-flight-before-you-publish).

**The order path is a linux/amd64 binary.** `execution.py:67` shells out to the
Alpaca CLI v0.0.14 (`README.md:81`, `cli_0.0.14_linux_amd64.tar.gz`). Any host
that runs *the agent* must be x86-64. This rules out ARM instances —
Hetzner CAX, Ampere, Graviton — unless a matching CLI build exists, which was
not checked. It does not constrain the *dashboard* host, which needs no CLI.

**The journal is WAL-mode SQLite with one writer and one reader.**
`journal.py:1401-1418` chose WAL specifically so "a reader (the dashboard) must
never block the writer (the agent)." A snapshot exporter is just another reader
and fits this design as-is.

**The payloads already carry their own staleness.** `_envelope`
(`dashboard.py:251`) stamps every response with `generated_at`, `data_as_of` and
`data_age_seconds`. The app was built to display honestly how old its view is,
which is exactly the property a snapshot architecture needs. See
[the staleness gotcha](#the-one-real-gotcha-staleness) for the one-line change
that keeps it honest.

---

## The WSL2 problem

This deserves its own section because it is the fact that decides the whole
question, and it is not obvious.

Microsoft's own documentation on systemd under WSL states:

> systemd services will **NOT** keep your WSL instance alive. Your WSL instance
> will stay alive in the same way it did previous to this update.

WSL2 supports systemd in 2026 (default on current Ubuntu via `wsl --install`;
otherwise `systemd=true` under `[boot]` in `/etc/wsl.conf` plus
`wsl.exe --shutdown`). But `systemctl enable cloudflared` does **not** mean the
tunnel runs whenever Windows is on. When no process holds the distro open, WSL2
shuts it down and everything inside dies with it — including cloudflared,
including the agent.

So the failure mode is not merely "the laptop sleeps at night." It is:

- Windows sleeps or hibernates → tunnel down.
- Windows updates and reboots overnight → tunnel down, distro not restarted.
- The operator closes the last WSL terminal → distro idles out → tunnel down.
- Wi-Fi switches networks → tunnel *probably* reconnects (outbound-only
  connections, exponential backoff at 1/2/4/8/16s, `--retries` default 5), but
  Cloudflare does not document behaviour after retries are exhausted. UNKNOWN.

Community workarounds exist (a Windows Task Scheduler entry running
`wsl.exe -d Ubuntu ...` at boot; keeping a process alive to pin the distro), but
they are community practice, not documented by Microsoft or Cloudflare, and they
do not survive the machine being off. **Do not bet a submission on them.**

What a judge sees when it is down: **Cloudflare error 1033**, the branded
"Cloudflare Tunnel error" interstitial — "Cloudflare's network cannot find a
healthy cloudflared instance to receive the traffic." Not a friendly maintenance
page. A Cloudflare-branded error, which reads as a broken submission.

And a custom offline page does not rescue it: Custom Error Rules are **0 on the
Free plan** (Pro 25, Business 50). Whether a Custom Error Rule can override the
1033 interstitial at all is UNKNOWN even on a paid plan.

### Separately: this is also a P&L risk

Out of scope for the dashboard, but it falls out of the same fact and the team
should see it. If the laptop sleeps or WSL idles out *during market hours*, the
agent stops trading and the exit monitor stops running with open positions on
the book. That is a trading-performance problem this deployment plan does not
solve. Whoever owns the run should decide, separately, whether the trading host
needs to change — and if it does, that decision needs to land before Monday
2026-08-31, not during the judged window.

---

## Options evaluated

### 1. Cloudflare Tunnel from WSL2

**Viable? Technically yes. As the submitted URL, no.**

A named tunnel **requires a domain on the Cloudflare account**. The dashboard
guide is explicit: "Before you publish an application through your tunnel, you
must add a website to Cloudflare." `cloudflared tunnel route dns` writes a CNAME
into a zone you control. If there is no domain on the account, a named tunnel
cannot produce a public hostname at all. **UNKNOWN: whether the operator has a
zone on their Cloudflare account.** Check before planning around this.

Quick tunnels (`trycloudflare.com`) need no account and no domain, but
Cloudflare's own docs disqualify them:

- "Quick Tunnels are intended for testing and development only."
- Hard cap of **200 concurrent in-flight requests**; excess returns `429`.
- **No Server-Sent Events support.**
- "We don't guarantee any SLA or uptime of TryCloudflare."
- **The subdomain is regenerated on every run.** Restart the process and the
  submitted URL is dead. For a URL frozen in a submission form four days before
  judging ends, that alone ends the discussion.

Docs note: Tunnel documentation moved to
`developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/`.
Remotely-managed (dashboard) tunnels are now the primary path; the local
`config.yml` workflow has been demoted to "do more with tunnels → local
management." The config format itself is unchanged.

Setup, if run as a supplement (requires a zone):

```bash
# install (Ubuntu/Debian amd64)
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared noble main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared

cloudflared tunnel login                                   # browser auth → ~/.cloudflared/cert.pem
cloudflared tunnel create underwriter                      # prints UUID, writes <UUID>.json
cloudflared tunnel route dns underwriter dash.example.com  # creates the CNAME
cloudflared tunnel ingress validate
cloudflared tunnel run underwriter
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <UUID>
credentials-file: /home/ianwalmsley/.cloudflared/<UUID>.json

ingress:
  - hostname: dash.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

The final catch-all rule with no `hostname` is mandatory. As a service (note the
explicit `--config`: under `sudo`, `$HOME` resolves to `/root` and the installer
looks in the wrong place):

```bash
sudo cloudflared --config /home/ianwalmsley/.cloudflared/config.yml service install
sudo systemctl start cloudflared
```

- **Effort:** 1–2 h including the DNS wait.
- **Cost:** $0. Free tier allows 1,000 tunnels/account, 25 replicas/tunnel, no
  documented bandwidth or request cap for named tunnels. A public hostname with
  no Access policy in front generates no authentication event and so should
  consume no Zero Trust seat — that inference is sound but is *not* a documented
  statement. UNKNOWN: the current free-plan seat count; Cloudflare no longer
  publishes it, and onboarding requires entering payment details even on Free.
- **TOS:** the old "section 2.8" media restriction now lives unnumbered in the
  Service-Specific Terms (updated 2026-06-02) and still bars serving "video or a
  disproportionate percentage of pictures, audio files, or other large files"
  through the CDN on Free/Pro/Business. An HTML+JSON dashboard is a non-issue.
- **Reliability over 4 judged days:** poor, for the reasons above.
- **Judge at 2am:** Cloudflare error 1033.

**Verdict: run it as a supplement for the demo video and live watching. Never
submit it.**

### 2. A VPS running both agent and dashboard

Genuinely fixes both the dashboard-uptime problem and the missed-cycles problem.
The cost is a migration two days before the first tradeable session.

Verified pricing, 2026-08-29:

| Provider | Cheapest realistic | Sleeps? | Notes |
|---|---|---|---|
| **Fly.io** | **$3.32/mo** (shared-cpu-1x, 512 MB) + $0.15/GB-mo volume | Only if you let it | `fly launch` writes `auto_stop_machines = "stop"` by default — **must** set `"off"` and `min_machines_running = 1` |
| **DigitalOcean** | **$6/mo** (1 GiB, 1 vCPU, 25 GiB, 1 TB) | No | The $4 tier (512 MiB) exists but risks an OOM kill with agent + uvicorn |
| AWS Lightsail | $5/mo | No | The $3.50 tier is **IPv6-only** — judges on v4-only networks cannot reach it |
| Render Starter | $7/mo | No | Disk binds to a single service |
| Railway Hobby | $5/mo incl. credit | Opt-in only | Trial accounts graded on GitHub account age; paying skips it |

**Avoid Hetzner for this deadline**, despite being the obvious price winner.
Two independent problems: the entire Cost-Optimized CX/CAX line currently renders
as "This product is currently unavailable" (the cheapest orderable US box is now
CPX11 at ~$21/mo), and signup runs real KYC via a third party with passport
upload — documented outcomes include outright rejection with orders cancelled,
and no published turnaround SLA. With four days left that tail risk is
unacceptable. (UNKNOWN: whether the unavailability is a stock flag or a
wind-down.)

**Avoid Oracle Always Free.** Halved on 2026-08-18 (4 OCPU/24 GB → 2 OCPU/12 GB)
on about two weeks' notice, with instances over the entitlement "automatically
terminated." It also has a documented idle-reclamation policy (7-day window,
<20% CPU/network/memory at p95) that a low-traffic judging dashboard matches
exactly.

**Avoid Render Free / anything that sleeps.** Render Free spins down after 15
minutes of no traffic and Render's own docs put the cold start at "about one
minute." A judge who waits a minute on a blank page has already formed a view.
Free web services also cannot attach a disk.

**Avoid Cloud Run for the agent.** Under request-based billing CPU is throttled
outside of requests, so the agent's background loop will not run reliably;
instance-based billing to fix that is ~$45/mo. And SQLite on GCS FUSE is a
corruption risk — Google's own docs say it "does not provide concurrency control
for multiple writes (file locking)… last write wins."

**What changes about the agent's operation if you move it:**

- Must be **x86-64** — the Alpaca CLI on the order path is a linux/amd64 binary.
- Alpaca *and* Anthropic API keys move onto a rented box. Blast radius grows;
  `.env` needs `chmod 600` and the paper-only guardrail (`ALPACA_LIVE_TRADE`
  unset/false, asserted in preflight) must be verified on the new host.
- Debugging mid-session becomes ssh. During a judged window that is a real cost.
- Python 3.12 + uv + the CLI binary all need installing and verifying against
  the 209-test suite before Monday.
- The clock is already derived from the exchange (`trading_day_of`,
  `EXCHANGE_TZ`), so host timezone is not a hazard — one thing that does *not*
  break.
- Fly's August 2026 status page logged ~10 incidents including capacity
  exhaustion preventing new machine creation in a region. "Couldn't create a
  machine" is a live 2026 deploy-day failure mode.

- **Effort:** 3–5 h to move the agent properly and re-verify. Not 1 hour.
- **Judge at 2am:** the dashboard is up and live. This is the only option that
  makes the *data* live at 2am too.

**Verdict: the right answer if the agent were being placed today. Too much
change, too close to Monday, to be the plan now.** Keep it in reach as
[Fallback B](#fallback-b-flyio-45-60-min) for the dashboard alone.

### 3. Split: agent local, journal replicated to a hosted dashboard

The intellectually appealing option, and the one that costs the most for the
least.

**Litestream → R2** is healthy and well-suited: v0.5.16 (2026-08-05), actively
released, R2 documented as a first-class S3-compatible target (v0.5.0+
auto-detects R2 endpoints and sets `sign-payload`/`concurrency` for you).
Critically, it is designed for exactly this shape — a standalone process
replicating a database another process writes, holding a long-running read
transaction and taking over checkpointing. Default `sync-interval` is 1s.
`litestream restore -f` now maintains a continuously-following read replica, and
a VFS extension can query the replica directly from object storage.

The problem is what it needs on the other end. **A Litestream read replica needs
a process with a filesystem — it does not run on Workers.** So this option is
option 2's VPS *plus* a replication layer, and it inherits every cost of option
2 while adding a second moving part. If a VPS is in play, run uvicorn against a
periodically-refreshed snapshot and skip the replication entirely.

Also relevant: replication would need the SQLite file on ext4, not `/mnt/d` —
DrvFs locking behaviour is a known source of trouble, and the global CLAUDE.md
already flags `/mnt/d` as slow.

**LiteFS is effectively dormant** — README still claims "actively maintained," but
the last commits to `main` are 2025-04-22 and are release plumbing. It is also
FUSE-based and aimed at multi-node clusters, architecturally wrong here. Don't.

**Cloudflare D1** is a workable mirror target: `POST /accounts/{id}/d1/database/{db}/query`
with a D1 Write token, free tier 100k rows written/day, 5M read/day, 500 MB per
database. Comfortable for a trading journal. But it means hand-maintaining a
second schema, idempotent upserts (no transactions across HTTP calls), a
watermark column, *and* writing a Worker to query it — 2–4 h to reproduce a view
the seven payload functions already produce. Watch the 1,200-requests-per-5-min
global Cloudflare API ceiling if you ever loop per-row.

**Workers KV is disqualified on consistency.** Free tier is **1,000 writes/day to
different keys** (a per-minute update during a 6.5 h session is ~390/day, so it
fits), but changes "may take up to 60 seconds or more to be visible in other
global network locations." That propagation window is the same order as the
refresh interval, with no ordering guarantee between consecutive updates. R2 has
strong per-object consistency, no daily write cap, and a bigger value ceiling for
identical effort. **Prefer R2 over KV, always, for this.**

**Turso** has a generous free tier (5 GB, 500M reads/mo) but is mid-rewrite —
libSQL's own README says "new features are being developed in Turso" and Turso
Database is a from-scratch Rust rewrite currently in beta. Not a platform to
adopt four days before a deadline.

- **Effort:** 3–6 h, and it still requires a hosted host.
- **Judge at 2am:** up, serving last-replicated state.

**Verdict: correct engineering for a problem this project does not have.** The
journal is a few MB over four days and the dashboard renders a fixed view. Full
database replication buys query flexibility nobody will use.

### 4. Static export — the recommendation

The agent renders the seven payloads to JSON on a timer and pushes them, with
`index.html`, to a static host.

Normally the objection is "you lose live-ness and you rewrite the frontend."
Neither applies here:

- **No frontend rewrite.** The page fetches seven bare paths; the export writes
  seven files at those paths. Nothing changes.
- **Little live-ness lost.** The underlying data changes at cycle cadence, not
  continuously, and the market is closed for most of the judged window. A
  snapshot pushed at the end of each cycle is as current as the journal is.
- **The staleness display already exists.** `_envelope` stamps every payload;
  the UI already renders "stale — view older than tolerance."

What is genuinely lost: the query parameters (`?limit=`, `?symbol=`,
`?cycle_id=`) that the API supports and the UI never sends. If anyone later adds
a drill-down that sends a query string, it breaks on static. Worth a comment in
the exporter.

- **Effort:** ~2 h.
- **Cost:** $0.
- **Reliability over 4 judged days:** the highest available. Cloudflare's edge
  serves eight immutable files. There is no origin to fail, no process to crash,
  no laptop in the path.
- **Judge at 2am:** the dashboard, fully rendered, showing data as of the last
  push with an honest "N hours ago" stamp.

### 5. Anything better?

**Datasette** (`datasette publish cloudrun`) is the best-trodden "SQLite file →
hosted read-only view" path and would give a queryable UI for free. But it
discards the purpose-built dashboard — the refusal taxonomy, the gates, the
shadow P&L — in favour of a generic table browser. The refusal display *is* the
project's argument. Wrong trade. (Also: 1.0 has been in alpha since Nov 2022;
current stable is 0.65.3. Datasette Cloud is preview-access only, no public
pricing.)

**`sql.js-httpvfs`** (ship the .sqlite, query it in-browser over range requests)
is a clever trick whose author describes it as "mainly written for small personal
projects of mine and as a demonstration," with no tests on the VFS layer. No.

Nothing beats option 4 on the metric that matters.

---

## Why Workers static assets, not Pages

Cloudflare now steers new projects away from Pages. The Pages landing page leads
with a callout titled "Are you sure you want to use Pages?":

> "Workers supports most Pages use cases and offers a broader feature set. It is
> Cloudflare's primary platform for building applications. **Start new projects
> with Workers.**"

Pages is not deprecated, and Direct Upload still works. But three specific things
make it the wrong choice *here*:

1. **The 500-deploys-per-month cap.** The limits page frames it around git
   pushes, but the Pages landing page calls it "500 deploys per month on the Free
   plan," and no doc states Direct Upload is exempt. At one push per 5-minute
   cycle you would generate thousands per month. **UNKNOWN whether Direct Upload
   counts — and that is precisely why not to risk it.** Workers has no equivalent
   documented cap.
2. **The SPA-fallback trap.** "If your project does not include a top-level
   `404.html` file, Pages assumes that you are deploying a single-page
   application" and returns `index.html` with HTTP 200 for any unmatched path.
   A missing or misspelled JSON path would return HTML with a 200, and
   `fetch().json()` would fail on `<!DOCTYPE` with a baffling error. Workers'
   default `not_found_handling: "none"` returns a plain 404.
3. **Extensionless Content-Type defaults are worse.** Pages serves an
   extensionless file as `application/octet-stream` (browser downloads it);
   Workers omits the header entirely. Both need fixing via `_headers`, but a
   judge clicking `/api/state` in the address bar gets a download prompt on
   Pages.

There is also a live wrangler behaviour worth knowing: **since wrangler 4.108.0,
`wrangler pages deploy` run by an AI coding agent against a brand-new purely
static project is silently delegated to Workers static assets** (detection via
the `am-i-vibing` package; `--force` opts out). Another reason to target Workers
deliberately rather than discover which platform you landed on.

Verified Workers static assets facts:

- **Asset requests are free and unlimited** on the free plan: "Requests to static
  assets are free and unlimited… There is no additional cost for storing Assets."
  A judge refreshing costs nothing. With no `main` script, *every* request is a
  static asset read and the 100k/day Workers request cap never engages. (Do not
  set `run_worker_first` — that forces billable Worker invocation.)
- **Free public URL, no domain required:** `<worker>.<subdomain>.workers.dev`.
- Limits: 20,000 files, 25 MiB per file, 100 `_headers` rules. Eight files under
  1 MB is not close to any of them.
- **Deploys are atomic.** A version "captures the complete state of your Worker…
  its bundled code, static assets, bindings," and `wrangler deploy` flips 100% of
  traffic to it in one step. You never serve a half-updated file set.
- Uploads are manifest-first: only files whose hash changed are re-uploaded, so
  `index.html` uploads once and each cycle ships only the changed JSON.
- **UNKNOWN: wall-clock deploy time and global propagation delay.** Cloudflare
  publishes no figure. Measure it on the first deploy.

One caveat: `workers.dev` "is intended for personal or hobby projects that aren't
business-critical." A hackathon dashboard qualifies. And set `"workers_dev": true`
explicitly — if you disable it in the dashboard without setting it in config, the
next `wrangler deploy` re-enables it.

---

## The one real gotcha: staleness

`_envelope` bakes `data_age_seconds` at generation time. In a live server that
is correct. In a snapshot it is a **lie**: a payload exported at 16:00 and viewed
at 02:00 still reports an age of a few seconds, and the UI would paint "within
tolerance" over ten-hour-old data. On a submission judged partly on honesty about
its own limitations, that is the worst possible bug to ship.

The fix is already in the frontend. `index.html:922`:

```js
if (age === null && s.generated_at) { var g = parseTime(s.generated_at); ... }
```

When `data_age_seconds` is `null`, the UI recomputes age client-side from
`generated_at`. So the exporter should **set `data_age_seconds` to `null` in
every exported payload** and leave `generated_at` and `data_as_of` intact. The
existing code path then computes true age at view time, and the "stale — view
older than tolerance" branch at `index.html:933` fires correctly for a judge
looking at 2am.

Verify this end-to-end before submitting: export a snapshot, change the system
clock or wait, load the page, and confirm it says the view is stale.

Consider also rendering a plain "snapshot taken at HH:MM ET" line in the footer,
so staleness is legible without reading the freshness widget.

---

## RECOMMENDATION

### Primary: static snapshot → Cloudflare Workers static assets

**Effort ~2 h. Cost $0. No domain needed. Up regardless of the laptop.**

#### Step 1 — write the exporter (~45 min)

A new module, e.g. `src/underwriter/export.py`. It imports from `dashboard.py`
and needs no new dependencies. Sketch — not tested, the constants and signatures
are from `dashboard.py:98-103, 165-176, 447-730`:

```python
"""Render the dashboard to static files for a snapshot host.

The dashboard's payload functions are pure, so the same bytes a live server
would return can be written to disk and served by any static host. The seven
paths below are exactly the ones static/index.html fetches; it sends no query
strings, so defaults are the whole contract.
"""

import json
import shutil
from pathlib import Path

from underwriter.dashboard import (
    DEFAULT_DECISION_LIMIT, DEFAULT_ORDER_LIMIT, DEFAULT_REJECTION_LIMIT,
    DashboardConfig, JournalGateway,
    decisions_payload, health_payload, orders_payload, pnl_payload,
    positions_payload, rejections_payload, state_payload,
)


def _honest(payload: dict) -> dict:
    """Drop the baked-in age so the page computes it at view time.

    index.html falls back to computing age from `generated_at` when
    `data_age_seconds` is null. In a snapshot the baked value would claim
    freshness hours after the fact.
    """
    payload["data_age_seconds"] = None
    return payload


def export(cfg: DashboardConfig, out: Path) -> None:
    now = cfg.clock()
    gateway = JournalGateway(cfg.journal_path)
    try:
        payloads = {
            "health": gateway.run(lambda j: health_payload(j, now=now)),
            "state": gateway.run(
                lambda j: state_payload(j, now=now, max_view_age=cfg.max_view_age)
            ),
            "positions": gateway.run(lambda j: positions_payload(j, now=now)),
            "decisions": gateway.run(
                lambda j: decisions_payload(j, now=now, limit=DEFAULT_DECISION_LIMIT,
                                            cycle_id=None, symbol=None)
            ),
            "rejections": gateway.run(
                lambda j: rejections_payload(j, now=now, limit=DEFAULT_REJECTION_LIMIT)
            ),
            "orders": gateway.run(
                lambda j: orders_payload(j, now=now, limit=DEFAULT_ORDER_LIMIT)
            ),
            "pnl": gateway.run(lambda j: pnl_payload(j, now=now, days=cfg.pnl_days)),
        }
    finally:
        gateway.close()

    api = out / "api"
    api.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        # No file extension: index.html fetches "/api/state", not "/api/state.json".
        (api / name).write_text(json.dumps(_honest(payload), separators=(",", ":")))

    shutil.copy2(cfg.static_dir / "index.html", out / "index.html")
```

Write it to a temp directory and move it into place, so a crashed export never
leaves half a file set for wrangler to ship.

#### Step 2 — the site directory (~10 min)

```
deploy/
  wrangler.jsonc
  public/
    _headers
    index.html      <- written by the exporter
    api/{health,state,positions,decisions,rejections,orders,pnl}
```

`deploy/wrangler.jsonc`:

```jsonc
{
  "name": "underwriter-dash",
  "compatibility_date": "2026-08-29",
  "assets": { "directory": "./public/" },
  "workers_dev": true
}
```

Do **not** add an `"binding": "ASSETS"` field — it is only valid alongside a
`main` script, and this Worker has none.

`deploy/public/_headers` — this is what fixes the extensionless Content-Type:

```
/api/*
  Content-Type: application/json; charset=utf-8
  Cache-Control: no-store
```

(`Access-Control-Allow-Origin: *` is only needed if anything fetches
cross-origin. Workers, unlike Pages, does not add CORS headers by default.)

Add `deploy/public/` and `deploy/.wrangler/` to `.gitignore` — generated output.

#### Step 3 — first deploy (~20 min)

```bash
cd /home/ianwalmsley/projects/alpaca/deploy
npx wrangler@latest login          # browser auth, once
npx wrangler@latest deploy
```

Note the URL it prints: `https://underwriter-dash.<subdomain>.workers.dev`.
**That is the submitted URL.** It is stable across every subsequent deploy.

#### Step 4 — verify (~15 min)

```bash
URL=https://underwriter-dash.<subdomain>.workers.dev

curl -sI  "$URL/api/state" | grep -i content-type   # must be application/json
curl -s   "$URL/api/state" | head -c 400
curl -sI  "$URL/api/nope"  | head -1                # must be 404, not 200
curl -sI  "$URL/" | head -1                         # 200, text/html
```

Then open it in a browser with the laptop's uvicorn **stopped**, and confirm all
six panels render from the static JSON. The Content-Type override is confirmed in
wrangler's source but not promised in the docs, so verify it rather than assume.

#### Step 5 — push on a timer (~20 min)

Simplest reliable form: a shell loop or cron entry that exports and deploys.
Run it from a WSL2 shell during market hours.

```bash
# scripts/publish.sh
set -euo pipefail
cd /home/ianwalmsley/projects/alpaca
uv run python -m underwriter.export --out deploy/public
cd deploy && npx wrangler@latest deploy
```

Cadence: once per trading cycle, or every 5 minutes during market hours. Nothing
in the limits pushes back at that rate — the binding constraint is the global
Cloudflare API ceiling of 1,200 requests per 5 minutes per user, and a deploy is
a handful of calls.

**The key property:** if this loop stops, nothing breaks. The last deployed
version keeps serving, correctly labelled as of its timestamp. A missed push is
a stale page, not a down page.

#### Step 6 — final push before submission

Push once more after the last trading session on Thursday 2026-09-03, so the URL
in the submission form shows the completed run rather than a mid-session state.

### Fallback A: R2 public bucket (~30 min)

If wrangler, Node, or the Workers auth flow fails on the day, the exporter's
output is unchanged — only the transport differs, and this one is pure Python.

1. Create an R2 bucket in the Cloudflare dashboard; enable the **r2.dev public
   subdomain** (no domain on the account required).
2. Create an R2 access key pair (S3-style, *not* a Cloudflare API token).
3. Upload with boto3 — `region_name="auto"` is required, and pass `ContentType`
   explicitly, which sidesteps the extensionless-file problem entirely:

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=KEY_ID,
    aws_secret_access_key=SECRET,
    region_name="auto",
)
for name in ("health", "state", "positions", "decisions", "rejections", "orders", "pnl"):
    s3.put_object(Bucket=BUCKET, Key=f"api/{name}",
                  Body=(out / "api" / name).read_bytes(),
                  ContentType="application/json; charset=utf-8",
                  CacheControl="no-store")
s3.put_object(Bucket=BUCKET, Key="index.html",
              Body=(out / "index.html").read_bytes(), ContentType="text/html")
```

Free tier: 10 GB storage, 1M Class A ops/month (`put_object` is Class A), 10M
Class B, **egress free**. Eight files every 5 minutes is ~115k Class A/month —
comfortably inside.

Two caveats:

- Cloudflare's docs say r2.dev "is rate-limited and should only be used for
  development purposes." **UNKNOWN: the actual threshold** — unpublished. For a
  handful of judges it is almost certainly fine; it is not a launch platform.
- **Public buckets have no root index document.** The dashboard lives at
  `https://pub-<hash>.r2.dev/index.html`, not at `/`. Submit the full path.
  Relative fetches to `/api/state` resolve correctly from that path.

`boto3` is not currently a dependency. If this fallback is likely to be needed,
adding it now costs nothing and removes an install from the critical path.

### Fallback B: Fly.io (45–60 min)

If Cloudflare as a whole is the problem, or if the team decides mid-week that a
live dashboard matters more than simplicity:

1. `fly launch` in the repo (Python 3.12 image, uvicorn as the process).
2. **Edit `fly.toml` before deploying:** set `auto_stop_machines = "off"` and
   `min_machines_running = 1`. The default is scale-to-zero, and Fly's own forum
   carries 2025–26 reports of FastAPI cold starts taking 23–60 s. A judge will
   not wait.
3. Attach a small volume; ship a snapshot of the journal to it:
   `sqlite3 journal.db "VACUUM INTO '/tmp/snap.db'"` then `fly sftp`. **Use
   `VACUUM INTO` or the backup API, never `cp`** — copying a live WAL-mode
   database gives an inconsistent file.
4. Re-upload the snapshot periodically, or leave it as an end-of-day view.

~$3.32/mo for shared-cpu-1x/512 MB plus $0.15/GB-mo for the volume. Use the
512 MB tier, not 256 MB — $1.30/mo is not worth an OOM kill. Note Fly logged ~10
incidents in August 2026 including regional capacity exhaustion blocking new
machine creation, so if this is the fallback, create the machine *early* even if
you do not use it.

---

## What to do about "laptop asleep"

**Nothing — because the recommended architecture removes the laptop from the
judge's path entirely.** That is the whole point of choosing option 4 over option 1.

Concretely:

| Time | Laptop | What a judge sees |
|---|---|---|
| Tue 14:30 ET | Awake, agent trading | Dashboard, data seconds old |
| Wed 02:00 ET | Off | Same dashboard, "last render" showing the previous session, freshness widget honestly reading stale |
| Thu 09:00 ET | Rebooting after Windows update | Same dashboard, unaffected |
| Fri 10:00 CDT | Whatever | Same dashboard, final state |

The only thing lost when the laptop is off is *newness*, and the page says so
itself. Nothing 404s, nothing shows a Cloudflare error, nothing spins.

Three things to actually do:

1. **Make staleness honest** — the `data_age_seconds = None` change above.
   Without it, the page claims freshness it does not have, which is worse than
   being visibly stale.
2. **Push a final snapshot after the last session** (Thu 2026-09-03) so the
   judged URL shows a completed run, not a half-finished Thursday.
3. **Do not submit a tunnel URL**, including as a "live" secondary link. A judge
   who clicks a secondary link at 2am and gets Cloudflare error 1033 has been
   handed evidence of a broken system, which costs more than the live view gains.
   If a live view matters for the demo video, record the video against the tunnel
   and submit the static URL.

---

## Pre-flight before you publish

The static export makes a file publicly readable forever. Check once, deliberately:

```bash
uv run python -m underwriter.export --out /tmp/check
grep -rEi 'PK[A-Z0-9]{16,}|sk-ant|secret|api_key|password' /tmp/check/api/ || echo "clean"
python -c "import json,glob; [json.load(open(f)) for f in glob.glob('/tmp/check/api/*')]" && echo "valid json"
```

Also confirm by eye:

- No account ID in any payload. The hackathon requires the paper account ID in
  the *submission form*; it does not need to be on the public page.
- No absolute filesystem paths (the journal-unreadable handler already withholds
  the DB path — check the happy path too).
- `_redact` is doing its job on any `context` blobs in decisions/rejections.

---

## Open questions

- **UNKNOWN: does the operator have a domain on their Cloudflare account?**
  Decides whether a named tunnel is even possible as a supplement. Does not
  affect the primary recommendation, which needs no domain.
- **UNKNOWN: wrangler deploy wall-clock time and propagation delay.** Not
  published. Measure on first deploy; it sets the real floor on push cadence.
- **UNKNOWN: r2.dev rate-limit threshold.** Documented as rate-limited, numbers
  unpublished. Only matters if Fallback A is used.
- **UNKNOWN: whether Pages Direct Upload counts against the 500-deploy cap.**
  Moot if Workers is used, which is why it is.
- **UNKNOWN: current Cloudflare Zero Trust free-plan seat count.** Not published
  on any live page. Only matters if a tunnel with an Access policy is used.
- **No git remote is configured** (`git remote -v` is empty). The submission
  requires a public GitHub repository — separate task, but it is on the critical
  path and nobody appears to own it yet.

---

## Sources

Cloudflare Tunnel · [overview](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) ·
[TryCloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) ·
[local tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/) ·
[config schema](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/) ·
[run as service](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/linux/) ·
[error 1033](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1033/) ·
[account limits](https://developers.cloudflare.com/cloudflare-one/account-limits/) ·
[custom errors](https://developers.cloudflare.com/rules/custom-error-responses/)

Workers & Pages · [Pages landing](https://developers.cloudflare.com/pages/) ·
[migrate from Pages](https://developers.cloudflare.com/workers/static-assets/migration-guides/migrate-from-pages/) ·
[static assets billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/) ·
[_headers](https://developers.cloudflare.com/workers/static-assets/headers/) ·
[workers.dev routing](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/) ·
[wrangler config](https://developers.cloudflare.com/workers/wrangler/configuration/) ·
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/) ·
[Pages limits](https://developers.cloudflare.com/pages/platform/limits/) ·
[Pages serving](https://developers.cloudflare.com/pages/configuration/serving-pages/)

Storage · [R2 pricing](https://developers.cloudflare.com/r2/pricing/) ·
[R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/) ·
[R2 + boto3](https://developers.cloudflare.com/r2/examples/aws/boto3/) ·
[KV limits](https://developers.cloudflare.com/kv/platform/limits/) ·
[how KV works](https://developers.cloudflare.com/kv/concepts/how-kv-works/) ·
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/) ·
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) ·
[API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/)

Replication · [Litestream](https://litestream.io/) ·
[Litestream config](https://litestream.io/reference/config/) ·
[S3-compatible guide](https://litestream.io/guides/s3-compatible/) ·
[LiteFS commits](https://github.com/superfly/litefs/commits/main) ·
[libSQL](https://github.com/tursodatabase/libsql) ·
[Turso pricing](https://turso.tech/pricing) ·
[Datasette publish](https://docs.datasette.io/en/stable/publish.html)

Hosts · [Fly pricing](https://fly.io/docs/about/pricing/) ·
[Fly autostop](https://fly.io/docs/launch/autostop-autostart/) ·
[Fly status](https://status.flyio.net) ·
[DO droplets](https://www.digitalocean.com/pricing/droplets) ·
[Hetzner price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/) ·
[Render free](https://render.com/docs/free) ·
[Railway pricing](https://railway.com/pricing) ·
[Lightsail](https://aws.amazon.com/lightsail/pricing/) ·
[Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) ·
[Cloud Run pricing](https://cloud.google.com/run/pricing)

Platform · [systemd on WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd) ·
[Cloudflare service-specific terms](https://www.cloudflare.com/service-specific-terms-application-services/)
