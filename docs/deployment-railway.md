# Railway as a host for the agent + dashboard

Research date: 2026-08-29. Judged window: Mon 31 Aug – Fri 4 Sep 2026.
Sources are Railway's own docs (`docs.railway.com`), `railway.com/pricing`,
`status.railway.com` and `railpack.com`. Anything not verifiable in those is
marked **UNKNOWN**.

**Verdict up front: Railway can host both processes, but as ONE service, not two.**
A Railway volume is attached to a single service, so the agent and the dashboard
cannot both mount the SQLite file as separate services. Run both in one container.

---

## 1. Pricing

Plans (from `docs.railway.com/reference/pricing/plans` and `railway.com/pricing`):

| Plan | Price | Included usage | Per-service limits | Volume cap |
|---|---|---|---|---|
| Free Trial | $0, **$5 one-time credit, 30 days**, no card | $5 once | 1 GB RAM, 2 vCPU, 2 replicas | 0.5 GB |
| Free | $0/mo | **$1/month** of credit | 0.5 GB RAM, 1 vCPU, 1 replica | 0.5 GB |
| Hobby | **$5/mo, includes $5/mo usage credit** | $5/mo, does not roll over | 48 GB RAM, 48 vCPU, 6 replicas, 7-day logs | 5 GB |
| Pro | $20/mo per workspace, includes $20 usage | $20/mo | 1 TB RAM, 1000 vCPU, 42 replicas | 50 GB (guide) / 1 TB (plans table) — docs disagree, see note |
| Enterprise | custom | — | — | 5 TB |

Usage rates (metered per second, billed on **actual** usage — "Pay only for what
your app uses, by the second. No overprovisioning, no idle markup"):

- RAM: **$10 / GB / month**
- CPU: **$20 / vCPU / month**
- Volume: **$0.15 / GB / month**
- Egress: **$0.05 / GB**

### What this workload actually costs

Single combined service (agent loop + uvicorn), always on, 730 h/month:

| Item | Assumption | Cost/mo |
|---|---|---|
| RAM | ~400 MB average RSS (Python 3.12 + anthropic + alpaca-py + uvicorn) | ~$4.00 |
| CPU | ~0.03 vCPU average (idle loop, bursts on decisions) | ~$0.60 |
| Volume | 1 GB | $0.15 |
| Egress | <1 GB (JSON dashboard, API calls) | ~$0.05 |
| **Usage total** | | **~$4.80** |

That lands just inside the Hobby $5 credit, so realistic all-in is **$5/month**,
worst case ~$7–8/month if memory runs higher than assumed. For the 5-day judged
window alone the metered usage is roughly **$0.80**.

Free tier: yes, but not usable here. The Free plan's $1/month credit buys roughly
2–3 days of a 0.4 GB always-on service, and caps the volume at 0.5 GB. The Trial's
$5 one-time credit would in fact cover the hackathon week — but with a 1 GB RAM /
0.5 GB volume cap and no card on file, it is a bad thing to be relying on when a
judge loads the dashboard. **Take Hobby at $5.**

---

## 2. Does anything sleep or scale to zero? — the decisive question

**No, not unless you turn it on.** Railway's sleeping feature is called
*Serverless* and it is **opt-in per service**:

> "Enabling Serverless on a service tells Railway to stop a service when it is
> inactive, effectively reducing the overall cost to run it."
> — `docs.railway.com/guides/optimize-usage`

You enable it at *Service settings → Deploy → Serverless → Enable Serverless*, and
it only takes effect on the next deployment. Leave it off and the container runs
continuously.

If it were on, the behaviour would be (from `docs.railway.com/deployments/serverless`):

> "Once a service stops sending packets it is considered inactive after 5 minutes"
> — "in practice a service sleeps somewhere between 5 and 10 minutes after its last
> outbound traffic."
> "A service is woken when it receives traffic from the internet or from another
> service in the same project through the private network."
> "The first request made to a slept service wakes it. It may take a small amount
> of time for the service to spin up again on the first request (commonly known as
> 'cold boot time')." First requests "may return a 502 Bad Gateway response."

Cold boot duration is not quantified in the docs — **UNKNOWN**, and irrelevant if
you never enable it. Note also that sleep is triggered by *outbound* packets, so a
service polling Alpaca every few seconds would never sleep anyway. **Action: verify
the Serverless toggle is off on the service before the judged window.**

There is no other idling, spin-down, or free-tier hibernation documented on any plan.

---

## 3. Persistent volumes

- Created via the project canvas or `⌘K`; on creation "you will be prompted to
  select a service to connect the volume to", then you set a mount path. Mounted at
  the absolute path you choose (mount at `/data`, read at `/data/…`).
- **"Each service can only have a single volume."** (`docs.railway.com/reference/volumes`)
- Sizes: 0.5 GB Free/Trial, 5 GB Hobby, 50 GB–1 TB Pro, 5 TB Enterprise. Resizing
  up is live and online; **"Down-sizing a volume is not currently supported."**
- Cost $0.15/GB/month, metered per minute.
- ~2–3% of capacity is consumed by filesystem metadata.
- IOPS: **"Read IOPS: 3,000 operations per second"**, **"Write IOPS: 3,000
  operations per second"** — far above what this workload needs.
- Backups: manual and automated backups are supported for services with volumes.
- Deleted volumes have a 48-hour restore grace period.

### Can two services share one volume? No.

The docs never describe attaching an existing volume to a second service; the
attach flow is volume → one service, and the stated caveat is one volume per
service. There is no documented multi-attach or shared-filesystem mode.

Strictly, the docs do not print the sentence "a volume cannot be shared between
services", so treat "definitively impossible" as **UNKNOWN**; but there is no
documented way to do it, and designing around it would be reckless for a judged
demo. Also relevant: **"we prevent multiple deployments from being active and
mounted to the same service"** — Railway actively serialises volume mounting, which
is the opposite of a shared-writer design.

**Therefore: one service running both processes**, sharing the volume in-process.
The alternative (two services, dashboard fetching over Railway's private network
from an HTTP endpoint on the agent) adds a second failure mode and a second
codepath for no benefit here.

---

## 4. SQLite on a Railway volume

- Survives restarts, redeploys and host migrations — that is what volumes are for.
  The failure mode to avoid is writing to the container filesystem instead: **"If
  your service requires data to persist between deployments … you should add a
  volume."** Ephemeral storage is wiped.
- **"Volumes are mounted to your service's container when it is started, not during
  build time"** — do not create or migrate the DB in a build step; do it at startup.
- Redeploys with a volume attached are **not** zero-downtime: "there will be a
  small amount of downtime when re-deploying a service that has a volume attached,
  even if there is a healthcheck endpoint configured." Expect a few seconds of gap.
  Don't redeploy mid-session with open positions unless you have to.
- WAL mode: safe here **because only one container ever mounts the volume**. WAL's
  known hazard is a network filesystem shared by multiple hosts; Railway prevents
  concurrent mounts. Both of our processes are in the same container, so the
  `-shm`/`-wal` files behave normally.
- `synchronous=FULL` is fine at 3,000 write IOPS.
- Whether the volume is local NVMe or network-attached block storage, and its exact
  fsync durability semantics, are **not documented** — **UNKNOWN**. Mitigation is
  the same either way: the agent must reconstruct state from the DB on boot, and
  you should take a volume backup before the judged window.
- Watch the 5 GB Hobby cap and the "100% capacity triggers an offline resize and
  restarts your service" behaviour. A trading journal won't get near it, but don't
  let debug logging land on the volume.

---

## 5. Long-running and scheduled processes

A Railway service is "a deployment target for your deployment source" — nothing
requires it to serve HTTP. A plain `python -m alpaca.agent` loop is a valid service;
worker services with no domain are normal on Railway.

**Cron: do not use it for the exit monitor.** Railway's cron
(`docs.railway.com/reference/cron-jobs`) runs the service's start command on a
schedule and requires the process to exit:

- Schedules are **UTC** (convenient — market hours are already 13:30–20:00 UTC).
- **"The shortest time between successive executions of a cron job cannot be less
  than 5 minutes."**
- **"If the code that runs in your Cron service does not exit, subsequent
  executions of the Cron will be skipped."** and "If a previous execution … has a
  status of `Active` … any new executions will not be run."

A 5-minute floor plus silent skipping of overlapping runs is wrong for an exit
monitor that must react within the session. Run one always-on process and do the
market-hours gating in Python (which you need anyway for holidays and early closes).

Whether a cron service can carry a volume is not stated — **UNKNOWN**, and moot.

**Restart on crash.** `docs.railway.com/reference/config-as-code` exposes
`restartPolicyType` with values `ON_FAILURE`, `ALWAYS`, `NEVER`, plus
`restartPolicyMaxRetries`. But the deployments reference states that a crashed
deployment goes to a `Crashed` state and stays there until manually restarted. The
documented default is not stated clearly — **UNKNOWN** — so **set it explicitly**:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "deploy": {
    "startCommand": "python -m alpaca.serve",
    "healthcheckPath": "/healthz",
    "restartPolicyType": "ALWAYS",
    "restartPolicyMaxRetries": 10,
    "numReplicas": 1
  }
}
```

`numReplicas` must stay at 1 — a second replica writing the same SQLite file is not
something you want, and Railway prevents two deployments mounting one volume anyway.

---

## 6. Deploy mechanics

- Sources: a **GitHub repo** (pick repo + branch; pushes redeploy), a **Docker
  image**, or the CLI. `railway link` connects a local directory to a project and
  `railway up` deploys the current directory (streaming logs, `--detach` to skip).
  Whether `railway up` requires git is not stated — **UNKNOWN**, but it uploads the
  directory, so a repo is not the only path.
- **No Dockerfile needed.** The default builder is **Railpack**: "Railway will
  always build with a Dockerfile if it finds one. New services default to Railpack
  unless otherwise specified."
- **Railpack understands uv.** From `railpack.com/languages/python`, package
  managers are detected by lockfile, and **uv is detected via `pyproject.toml` +
  `uv.lock`** (alongside pip/poetry/pdm/pipenv).
- Python version priority: `RAILPACK_PYTHON_VERSION` env var → mise-compatible files
  (`.python-version`, `.tool-versions`, `mise.toml`) → `runtime.txt` → `Pipfile` →
  **default 3.13.2**. Supported: "Python 3.10 and later". **To pin 3.12, commit a
  `.python-version` containing `3.12`** — otherwise you silently get 3.13.
- Railpack infers a start command (it recognises FastAPI and would run
  `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`). Since we want a custom
  entrypoint running both processes, set `startCommand` explicitly in
  `railway.json` rather than relying on inference.
- Healthchecks: point `healthcheckPath` at a dashboard endpoint; Railway polls until
  it gets a 200 before marking the deploy active. Default healthcheck timeout is
  **300 seconds**, overridable with `RAILWAY_HEALTHCHECK_TIMEOUT_SEC`.
- Shutdown: the old deployment gets SIGTERM with a grace period set by
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`. Handle SIGTERM so the agent closes the DB
  cleanly.

---

## 7. Public URL

- A service gets a Railway-provided `*.railway.app` domain via *Settings → Public
  Networking → Generate Domain*. Not automatic — you click it once.
- **HTTPS is automatic**: "Free SSL certificates automatically provisioned and
  renewed", no configuration.
- **A custom domain is not required.**
- Whether the generated subdomain is guaranteed stable across redeploys is not
  stated in the docs — **UNKNOWN** — but the domain is a property of the service,
  not of a deployment, so it persists for the life of the service. It changes if
  you delete and recreate the service. Don't recreate the service mid-hackathon.
- Bind uvicorn to `0.0.0.0` and the injected `$PORT`.

---

## 8. Secrets

- Set per-service under *Variables*, or as shared variables at project level.
  Also settable from the CLI (`railway variable set KEY=value`).
- Available to "the build process for each service deployment", "the running
  service deployment", `railway run <COMMAND>` and `railway shell` — so they reach
  the process as ordinary env vars, and nothing is baked into the repo or image
  unless you put it there.
- They live on the service, not the deployment, so they persist across redeploys.
- **Sealed variables** are the hardening option: once sealed, "its value is
  provided to builds and deployments but is never visible in the UI nor can it be
  retrieved via the API." Caveats: **"Sealed variables cannot be un-sealed"**, and
  they are excluded from PR environments, environment duplication, and
  `railway variables`. For a hackathon, seal the Alpaca secret and the Anthropic
  key only once you're confident you won't need to read them back.
- Whether variables can leak into build output is not documented — **UNKNOWN**.
  Assume anything you `print()` reaches the 7-day log history; don't log key values.

---

## 9. Reliability risk for the judged window

From `status.railway.com` on 2026-08-29: **all systems fully operational.** 90-day
uptime is 100% for Dashboard, API, Builds, Deployments, Compute, Auth, Storage and
Payments, and 100% for EU West (Amsterdam) and Southeast Asia (Singapore). The two
US regions and Public Networking sit at **99.80% over 90 days**, dipping to 99.42%
in July — roughly 4 hours of degradation in that month. No individual incident
records with dates or durations are published on the page, so per-incident detail
is **UNKNOWN**. Note the caveat: "This status page reports incidents with
significant, widespread user impact" — small blips may not appear.

The risk that would take the service down without any action from us:

> Railway migrates services between hosts for workload optimisation, security and
> performance updates (with advance notice), and host failures. These are
> **"mandatory and cannot be opted out of."**

So plan for the container being restarted at an arbitrary moment during the judged
window. Concretely:

1. The agent must reconstruct all open-position and exit-monitor state from SQLite
   on boot, with no in-memory-only state that matters.
2. `restartPolicyType: ALWAYS` so a crash comes back by itself.
3. Take a volume backup on Sun 30 Aug, before judging opens.
4. Freeze deploys during market hours — volume-attached redeploys carry downtime.
5. Deploy to **EU West (Amsterdam)** if region choice is offered; it is the only
   region at 100% over 90 days and its latency to Alpaca is irrelevant for a paper
   options strategy on a minutes-scale loop. If US latency matters for your fills,
   US East is still 99.80%.

---

## Verdict and recommended shape

**Railway is a good fit for both processes — as a single service.** The pricing is
right (~$5/month all-in on Hobby), nothing sleeps unless you switch sleeping on,
volumes are genuinely durable, and Railpack builds a `uv` project with no
Dockerfile. The one hard constraint is that a volume belongs to one service, which
rules out the two-service split.

Recommended setup:

- **One Railway project, one service, one volume.**
- Volume mounted at `/data`, 1 GB (Hobby allows 5 GB). SQLite lives at
  `/data/alpaca.db`; keep WAL and `synchronous=FULL`.
- Service start command runs a small Python entrypoint that starts uvicorn (bound
  to `0.0.0.0:$PORT`) and the agent loop in the same process tree — either uvicorn
  in a thread with the agent on the main loop, or both as asyncio tasks. Both read
  and write the same SQLite file via ordinary local file access, no network hop.
- `railway.json` with `restartPolicyType: ALWAYS`, `numReplicas: 1`, and
  `healthcheckPath` pointed at a cheap dashboard endpoint.
- `.python-version` pinned to `3.12`, `uv.lock` committed so Railpack uses uv.
- Serverless **off**. Verify the toggle before Monday.
- Generate the public domain once and never recreate the service; HTTPS is free.
- Alpaca and Anthropic keys as service variables, sealed once stable.
- Deploy from GitHub for reproducibility, keep the CLI available for an emergency
  `railway up` and for `railway logs`.
- No Railway cron. Market-hours gating (13:30–20:00 UTC, plus holidays and early
  closes) belongs in the agent, which needs it regardless.

Fallback if the single-service shape ever needs splitting: put the agent on the
volume-owning service and have the dashboard call it over Railway's project-private
network rather than trying to share the file. That is a bigger change than it looks
and is not worth doing before the hackathon.

### Open items (UNKNOWN)

- Exact cold-boot duration for Serverless (moot — leave it disabled).
- Whether one volume can be force-attached to a second service via the API.
- Volume storage backend (local vs network-attached) and precise fsync durability.
- Railway's default `restartPolicyType` — set it explicitly rather than find out.
- Whether generated `*.railway.app` subdomains are contractually stable across
  redeploys (they are per-service, so in practice yes).
- Pro-plan volume cap: the plans table says 1 TB, the volumes guide says 50 GB.
  Irrelevant on Hobby.
- Per-incident history behind the status page's 99.80% US-region figure.
