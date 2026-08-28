# Alpaca Tooling Recon — 2026-08-28

Research for the Alpaca AI Trading Agents hackathon build (2026-08-28 → 2026-09-04).

Everything below was verified live on 2026-08-28 against GitHub / PyPI, or by actually
installing and running the code locally. Anything I could not verify is marked
**UNKNOWN** rather than guessed. Where a claim is inference rather than observation,
it says so.

---

## 1. alpaca-py

**Current version: `0.44.0`** (latest PyPI release; latest git tag `v0.44.0`).

### requires-python — exact values

| Source | Value |
| --- | --- |
| PyPI metadata `requires_python` | `">=3.10.0,<4.0.0"` |
| `pyproject.toml` `[tool.poetry.dependencies]` | `python = "^3.10.0"` (caret == same range) |
| PyPI classifiers | `Programming Language :: Python :: 3`, `3.10`, `3.11`, `3.12`, `3.13`, `3.14` |

**Python 3.12: SUPPORTED.** Inside the declared range and explicitly classifier-listed.
Caveat: I did **not** empirically test 3.12 in this session, and it is not in upstream CI.

**Python 3.14: SUPPORTED — and I verified it empirically, because the metadata alone is
not trustworthy evidence.**

Why the metadata alone is not enough: `pyproject.toml` only declares `python = "^3.10.0"`.
Poetry auto-generates the whole `3.10 … 3.14` classifier list from that one constraint, so
the `3.14` classifier is *permission*, not a tested-compatibility claim. Meanwhile
`.github/workflows/ci.yaml:34` reads:

```yaml
matrix:
  python-version: [ "3.10", "3.11" ] #we'll want to add other versions down the road
  os: [ ubuntu-latest ]
```

So upstream tests **only 3.10 and 3.11**. 3.12, 3.13 and 3.14 are untested by Alpaca.

So I installed and ran it. On local CPython **3.14.4**:

- `uv pip install alpaca-py` resolved cleanly. All 19 deps came down as **wheels**, no
  source builds: `alpaca-py==0.44.0`, `pandas==3.0.5`, `numpy==2.5.2`, `pydantic==2.13.5`,
  `pydantic-core==2.46.5`, `msgpack==1.2.2`, `websockets==17.1`, `requests==2.34.2`,
  `sseclient-py==1.9.0`, `pytz==2026.3.post1`, `urllib3==2.7.0`, `certifi==2026.7.22`.
- Every class listed in §1.3–1.5 below imports successfully.
- I constructed a real 2-leg `mleg` order and serialized it via `to_request_fields()` —
  correct output, enums resolved properly.
- `BarSet.df` renders correctly under **pandas 3.0.5**. This was the risk actually worth
  checking: alpaca-py declares only `pandas>=1.5.3`, so it silently floats you onto the
  pandas 3.x major that upstream CI has never exercised. The DataFrame path works.
- No open alpaca-py issues mention Python 3.14. (Only relevant open issue is #644,
  "`alpaca.data.requests` imports pytz but package does not depend on pytz" — stale;
  `pytz>=2020.1` *is* in `requires_dist` for 0.44.0.)

**Recommendation: pin Python 3.14 with confidence, but pin `pandas` explicitly too.**
The untested-major exposure is real even though the DataFrame path passes today.

Reproduction venv still on disk at:
`/tmp/claude-1000/-home-ianwalmsley-projects-alpaca/8645b884-c4d6-46b4-975e-929328a76b90/scratchpad/py314/`

### Dependencies (`requires_dist`, 0.44.0)

```
msgpack<2.0.0,>=1.0.3
pandas>=1.5.3
pydantic<3.0.0,>=2.0.3
pytz>=2020.1
requests<3.0.0,>=2.30.0
sseclient-py<2.0.0,>=1.7.2
websockets>=10.4
```

### Multi-leg option order — exact class and method names

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent

TradingClient(api_key=None, secret_key=None, oauth_token=None,
              paper=True, raw_data=False, url_override=None)

TradingClient.submit_order(order_data: OrderRequest) -> Order

LimitOrderRequest(
    qty=1,
    limit_price=1.25,
    order_class=OrderClass.MLEG,
    time_in_force=TimeInForce.DAY,
    legs=[
        OptionLegRequest(symbol="SPY260918C00500000", ratio_qty=1,
                         side=OrderSide.BUY,  position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol="SPY260918C00510000", ratio_qty=1,
                         side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
    ],
)
```

`OptionLegRequest` fields (`alpaca/trading/requests.py:169`):

```python
symbol: str
ratio_qty: float
side: Optional[OrderSide] = None
position_intent: Optional[PositionIntent] = None
```

Its validator requires **at least one of `side` / `position_intent`**.

Validator rules on `OrderRequest` (`alpaca/trading/requests.py:385-399`) when
`order_class == OrderClass.MLEG`:

- `legs` is required (`"legs is required for the mleg order class."`)
- **at least 2 legs**, **at most 4 legs**
- all leg symbols must be **unique** (`"All legs must have unique symbols."`)

Verified serialization output on 3.14.4:

```python
{'qty': 1.0, 'type': <OrderType.LIMIT: 'limit'>, 'time_in_force': <TimeInForce.DAY: 'day'>,
 'order_class': <OrderClass.MLEG: 'mleg'>,
 'legs': [{'symbol': 'SPY260918C00500000', 'ratio_qty': 1.0,
           'side': <OrderSide.BUY: 'buy'>,
           'position_intent': <PositionIntent.BUY_TO_OPEN: 'buy_to_open'>},
          {'symbol': 'SPY260918C00510000', 'ratio_qty': 1.0,
           'side': <OrderSide.SELL: 'sell'>,
           'position_intent': <PositionIntent.SELL_TO_OPEN: 'sell_to_open'>}],
 'limit_price': 1.25}
```

Other order-request classes, all subclasses of `OrderRequest` and all accepting `legs`:
`MarketOrderRequest`, `LimitOrderRequest`, `StopOrderRequest`, `StopLimitOrderRequest`,
`TrailingStopOrderRequest`.

### Option contracts — Trading API, on `TradingClient`

```python
from alpaca.trading.requests import GetOptionContractsRequest

TradingClient.get_option_contracts(request: GetOptionContractsRequest) -> OptionContractsResponse
TradingClient.get_option_contract(symbol_or_id: UUID | str)          -> OptionContract
TradingClient.exercise_options_position(symbol_or_contract_id: UUID | str) -> None
```

`GetOptionContractsRequest` fields:
`underlying_symbols`, `status`, `expiration_date`, `expiration_date_gte`,
`expiration_date_lte`, `root_symbol`, `type`, `style`, `strike_price_gte`,
`strike_price_lte`, `limit`, `page_token`

### Option chains — Market Data API, a **different** client

```python
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionSnapshotRequest, OptionBarsRequest

OptionHistoricalDataClient(api_key=None, secret_key=None, oauth_token=None,
                           use_basic_auth=False, raw_data=False,
                           url_override=None, sandbox=False)

  .get_option_chain(request_params: OptionChainRequest)     -> Dict[str, OptionsSnapshot]
  .get_option_snapshot(request_params: OptionSnapshotRequest) -> Dict[str, OptionsSnapshot]
  .get_option_bars(request_params: OptionBarsRequest)       -> BarSet
  .get_option_latest_quote(request_params: OptionLatestQuoteRequest) -> Dict[str, Quote]
  .get_option_latest_trade(...)
  .get_option_trades(...)
  .get_option_exchange_codes() -> RawData
```

`OptionChainRequest` fields:
`underlying_symbol`, `feed`, `type`, `strike_price_gte`, `strike_price_lte`,
`expiration_date`, `expiration_date_gte`, `expiration_date_lte`, `root_symbol`,
`updated_since`

> **Gotcha:** option *contracts* come from `TradingClient`, option *chains* come from
> `OptionHistoricalDataClient`. Two separate clients, two separate constructions.

### Streams

```python
from alpaca.trading.stream import TradingStream
from alpaca.data.live.option import OptionDataStream
from alpaca.data.live.stock import StockDataStream

TradingStream(api_key, secret_key, paper=True, raw_data=False,
              url_override=None, websocket_params=None)
  .subscribe_trade_updates(handler: Callable)   # NOTE: method is subscribe_trade_updates,
                                                # not `trade_updates`
  .run() / .stop() / .close() / .stop_ws()

OptionDataStream(api_key, secret_key, raw_data=False,
                 feed=OptionsFeed.INDICATIVE, websocket_params=None,
                 url_override=None, data_timeout=None)
  .subscribe_quotes(handler, *symbols)   / .subscribe_trades(handler, *symbols)
  .unsubscribe_quotes(*symbols)          / .unsubscribe_trades(*symbols)
  .run() / .stop() / .close() / .stop_ws()
```

Two gotchas:

1. `OptionDataStream` has **no `paper` parameter** (unlike `TradingStream`).
2. It defaults to `feed=OptionsFeed.INDICATIVE`. Real quotes need `OptionsFeed.OPRA`,
   which requires the paid data subscription.

---

## 2. alpacahq/alpaca-skills

<https://github.com/alpacahq/alpaca-skills> — Apache-2.0.

These are **plain agent-agnostic `SKILL.md` files**, *not* Claude Code plugins and not an
MCP server. **14 skills** in the tree (the README's table lists 13 — `broker-api/journals`
is present in the tree but omitted from the README table).

### Top-level structure (verified via GitHub trees API)

```
.github/
  ISSUE_TEMPLATE/{bug_report.yml,config.yml,feature_request.yml}
  PULL_REQUEST_TEMPLATE.md
  workflows/skill-check.yml
.gitignore
AGENTS.md
CODEOWNERS
CONTRIBUTING.md
LICENSE
README.md
SECURITY.md
scripts/validate_skills.py
skills/
  trading-api/
    backtest/            {SKILL.md, reference.md}
    paper-trading/       {SKILL.md, reference.md}
    paper-trading-cli/   {SKILL.md, reference.md}
    paper-trading-mcp/   {SKILL.md, reference.md}
  broker-api/
    README.md
    integration/                  {SKILL.md, reference.md}
    account-onboarding/           {SKILL.md, reference.md}
    funding-transfers/            {SKILL.md, reference.md}
    journals/                     {SKILL.md, reference.md}
    trading-orders/               {SKILL.md, reference.md}
    market-data/                  {SKILL.md, reference.md}
    sse-events/                   {SKILL.md, reference.md}
    reconciliation-idempotency/   {SKILL.md, reference.md}
    rate-limits-resilience/       {SKILL.md, reference.md}
    money-precision/              {SKILL.md, reference.md}
templates/skill/{SKILL.md, reference.md}
```

Every skill directory contains exactly two files: `SKILL.md` and `reference.md`.

### Install instructions (verbatim from README lines 19-53)

Recommended — Skills CLI:

```bash
# Interactive install
npx skills add alpacahq/alpaca-skills

# Preview available skills
npx skills add alpacahq/alpaca-skills --list

# Install one specific skill
npx skills add alpacahq/alpaca-skills --skill alpaca-trading-backtest
```

Manual install — README's own table:

| Agent | Typical path |
| --- | --- |
| **Cursor** | Copy or symlink a skill directory into `.cursor/skills/` (project) or your user skills directory |
| **Claude Code** | Copy into `~/.claude/skills/` |
| **Other** | Reference the `SKILL.md` path directly in your agent prompt |

README's own example:

```bash
mkdir -p .cursor/skills
cp -r path/to/alpaca-skills/skills/trading-api/backtest .cursor/skills/alpaca-trading-backtest
```

> **Where skills get installed for us: `~/.claude/skills/`.**
>
> **Trap:** the *directory* name differs from the *skill* name. `skills/trading-api/backtest/`
> installs as `alpaca-trading-backtest`. The skill name is the `name:` field in the SKILL.md
> frontmatter. A manual `cp -r` **must rename the directory to the skill name** — note that
> Alpaca's own example above does exactly this rename.

### Skill name → path table (verbatim from README)

| Name | Path | Title | Product |
| --- | --- | --- | --- |
| `alpaca-trading-backtest` | `skills/trading-api/backtest/` | Trading API Backtesting | Trading API |
| `alpaca-trading-paper-trading` | `skills/trading-api/paper-trading/` | Paper Trading | Trading API |
| `alpaca-trading-paper-trading-cli` | `skills/trading-api/paper-trading-cli/` | Paper Trading (CLI) | Trading API |
| `alpaca-trading-paper-trading-mcp` | `skills/trading-api/paper-trading-mcp/` | Paper Trading (MCP Server) | Trading API |
| `alpaca-broker-integration` | `skills/broker-api/integration/` | Broker API Integration | Broker API |
| `alpaca-broker-account-onboarding` | `skills/broker-api/account-onboarding/` | Account Onboarding & KYC | Broker API |
| `alpaca-broker-funding-transfers` | `skills/broker-api/funding-transfers/` | Funding & Transfers | Broker API |

(remaining broker rows follow the same `alpaca-broker-*` pattern: `journals`,
`trading-orders`, `market-data`, `sse-events`, `reconciliation-idempotency`,
`rate-limits-resilience`, `money-precision`)

### The four Trading-API skills, summarized

**`alpaca-trading-backtest`** — deterministic, reproducible historical backtests.
Documented pipeline:

```
strategy idea -> formalized rules -> confirmed assumptions -> CLI data fetch
             -> local script -> artifacts -> report
```

**Hard-requires the Alpaca CLI** for market-data access (`alpaca version` to check;
`brew install alpacahq/tap/cli`, or `go install github.com/alpacahq/cli/cmd/alpaca@latest`).
The agent writes minimal local workspace code. Mandates a disclosure block in every
`notes.md` / `report.md` / `summary.json` / notebook / dashboard, and when modelling fees
requires linking `https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf` plus
recording the PDF revision date, extraction timestamp, modeled fee categories, and
excluded fee items.

**`alpaca-trading-paper-trading`** — the **generic, implementation-agnostic** version.
Explicitly tool-independent: works with alpaca-py, raw REST, JS/TS, Go, C#, or any agent
tool that reaches the Trading API. Defines a 10-step workflow: identify signal source,
restate strategy and confirm, gather *all* order parameters explicitly, confirm which
paper account (options approval level / crypto enabled / margin vs cash / PDT), mandatory
preview table before every submission, ask confirmation preference (**default ON**),
submit paper-only, return full post-submission details, monitor lifecycle
(filled / partially filled / rejected / canceled), never place live trades. The live
check is a **hard block, not a soft warning**: base URL without the `paper-` prefix, or a
profile set to live, stops execution.

→ **This is the one to install if we drive alpaca-py from our own code.**

**`alpaca-trading-paper-trading-cli`** — same workflow bound to the `alpaca` CLI.
Environment gate is `alpaca doctor`, whose `Trading:` line must read
`https://paper-api.alpaca.markets`. Submits via `alpaca order submit`, monitors via
`alpaca order get`. Note it flags the CLI as **Alpha Preview** — "Commands, flags, and
output formats may change between releases, which is why your agent discovers flags at
runtime rather than trusting any list in this file."

**`alpaca-trading-paper-trading-mcp`** — same workflow via MCP tool calls. Notable
instructions: call `GetDynamicTools` to discover the namespace and inspect schemas rather
than assuming tool names; verify paper mode by reading `env.ALPACA_PAPER_TRADE` **from the
host's MCP config file, because no tool exposes it**; order placement is **split across
separate stock / crypto / option tools**, selected by asset class. Its config example
targets `~/.cursor/mcp.json` with `uvx alpaca-mcp-server`.

---

## 3. alpacahq/alpaca-mcp-server

<https://github.com/alpacahq/alpaca-mcp-server> — **v2.3.0**, MIT.

- `requires-python = ">=3.10"`
- Dependencies: `fastmcp>=3.1.0`, `httpx>=0.27.0`, `python-dotenv>=1.0.0`, `click>=8.1.0`
- Install: `uvx alpaca-mcp-server`
- All configuration is environment variables set in the MCP client config.
  README: "No files are written to disk."
- **Verified: installs and imports cleanly on Python 3.14.4** (pulls `fastmcp 3.4.7`).

### Environment variables (exact, README config table)

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ALPACA_API_KEY` | Yes | — | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Yes | — | Your Alpaca secret key |
| `ALPACA_PAPER_TRADE` | No | `true` | Set to `false` for live trading |
| `ALPACA_TOOLSETS` | No | all | Comma-separated list of toolsets to enable |

Two more appear elsewhere in the README:

- `ALPACA_MCP_USER_AGENT` — optional; can be set empty to opt out of tracking.
- `ALPACA_RUN_README_INTEGRATION` — test-harness only.

Example client config:

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "your_key",
        "ALPACA_SECRET_KEY": "your_secret",
        "ALPACA_TOOLSETS": "stock-data,crypto-data"
      }
    }
  }
}
```

### ALPACA_TOOLSETS — all 11 toolset names

| Toolset | Description |
| --- | --- |
| `account` | Account info, config, portfolio history, activities |
| `trading` | Orders, positions, exercise options |
| `watchlists` | Watchlist CRUD operations |
| `assets` | Asset lookup, option contracts, calendar, clock |
| `stock-data` | Stock bars, quotes, trades, snapshots, screeners |
| `crypto-data` | Crypto bars, quotes, trades, snapshots, orderbooks |
| `options-data` | Option bars, quotes, trades, snapshots, chain, exchange codes |
| `corporate-actions` | Corporate action announcements |
| `news` | News articles for stocks and crypto |
| `fixed-income-data` | Fixed income (bond/treasury) quotes |
| `locates` | Short-sale locate requests and quotes |

For options work specifically: **chains and Greeks are in `options-data`; option
*contracts* are in `assets` (not `trading`); order placement is in `trading`.**

### The multi-leg `legs` serialization bug — STILL OPEN

**Yes, still open.** Verified three independent ways:

**(a) Issue #97** — <https://github.com/alpacahq/alpaca-mcp-server/issues/97>
"Multi-leg place_option_order rejects legs as string instead of array; time_in_force
docstring inaccurate." State **open**, created 2026-07-01T17:16:59Z, **updated
2026-07-01T17:16:59Z** (i.e. untouched since it was filed), **no labels**, no maintainer
response. Failure:

```
1 validation error for call[place_option_order]
legs
  Input should be a valid list [type=list_type,
   input_value='[{"symbol":"SPY260731P00...intent":"buy_to_open"}]', input_type=str]
```

Reporter confirmed the identical payload POSTed directly to
`https://paper-api.alpaca.markets/v2/orders` returns **200** with legs correctly
populated — so the Alpaca REST API and the server's Python signature are both fine; the
break is the MCP client stringifying the array argument in transit.

**(b) PR #107** — <https://github.com/alpacahq/alpaca-mcp-server/pull/107>
"Accept JSON string legs for option orders", `Fixes #97`. GitHub API reports:
`state: open`, `merged: false`, `mergeable_state: clean`, base `main`, head
`bozarnr:agent/parse-option-legs-json` (a community fork), last updated 2026-08-24.
**Proposed but NOT merged.**

**(c) I checked `main` directly** rather than trusting the issue text —
`src/alpaca_mcp_server/overrides.py`:

- line 268: `legs: Optional[list[dict]] = None` — unchanged
- **no `json.loads` anywhere in the file**
- lines 338-339: `if legs is not None: body["legs"] = legs` — a bare passthrough, no
  string handling

Secondary bug from #97, also still live: line 284 docstring reads
`time_in_force: "day" only. Options do not support other values.` This is **inaccurate** —
the code applies no such restriction (passes it straight through) and Alpaca's API accepts
`gtc` for options. It will steer an LLM caller away from GTC for no reason.

**→ Do not route multi-leg option orders through the local MCP server.**

### Other open issues worth knowing

- **#45** "The context size is huge" — all 11 toolsets enabled is a lot of tool schema.
  Trim with `ALPACA_TOOLSETS`.
- **#33** "Unexpected error placing option order: name 'OrderType' is not defined"
- **#79** Request for `.mcpb` extension or hosted remote MCP for current Claude Desktop builds
- **#43** Docker build fails: httpcore==1.0.10 not found (404 from PyPI)
- **#101** feat: optional independent pre-trade review middleware

---

## 4. Alpaca CLI v0.0.14 — `--legs` CONFIRMED WORKING

The team lead reported another agent found CLI v0.0.14 shipped today with a working
`--legs` flag. **I independently verified this and it holds up.** Since we're writing code
against it, here are the specifics.

Release: `v0.0.14`, published **2026-08-28T16:23:08Z**, `prerelease: false`.
Changelog body is just two commits:

```
53606273 Regenerate CLI from latest OAS (#13)
f5f7083  fix: standardize User-Agent header across API client, OAuth, and update checks (#12)
```

"Regenerate CLI from latest OAS" is consistent with `--legs` appearing: the command
surface is generated from the OpenAPI specs vendored at `api/specs/trading-api.json`.

The flag is real. `internal/cmd/commands.gen.go:541-544` at tag `v0.0.14`:

```go
if cmdutil.Changed(cmd, "legs") {
    if err := json.Unmarshal([]byte(cmdutil.Str(cmd, "legs")), &body.Legs); err != nil {
        return nil, fmt.Errorf("--legs: %w", err)
    }
}
```

`--legs` takes a **JSON string** and unmarshals it into `body.Legs`. Target type
(`internal/api/trading_types.gen.go:440`):

```go
type MLegOrderLeg struct {
    PositionIntent PositionIntent `json:"position_intent,omitempty"`
    RatioQty       string         `json:"ratio_qty"`
    Side           OrderSide      `json:"side,omitempty"`
    Symbol         string         `json:"symbol"`
}
```

Note `ratio_qty` is a **string** here, whereas alpaca-py's `OptionLegRequest.ratio_qty`
is a `float`.

Other `alpaca order submit` fields it builds from flags: `--order-class`,
`--position-intent`, `--qty`, `--side`, `--stop-price`, `--symbol`, `--time-in-force`,
`--trail-percent`, `--trail-price`, `--type`, `--advanced-instructions` (also JSON),
`--take-profit` / `--stop-loss` (which auto-set `order_class: bracket`).

Also useful — `internal/cmd/order.go:13-14` adds a hand-written flag:

```go
cmd.Flags().Bool("dry-run", false, "Print the request body without submitting")
```

`--dry-run` prints the request body without submitting. That is a genuinely good preview
gate for an agent loop, and it satisfies the paper-trading skill's mandatory
preview-before-submit requirement cheaply.

`postOrderHook` also defaults `time_in_force` when unset: `gtc` if the symbol contains
`/` (crypto), else `day`.

### Does this change the read on needing the SDK for the order path?

**It makes the CLI viable, but I would still put alpaca-py on the order path. Recommendation unchanged.**

What the CLI genuinely fixes: it is the *same* fix PR #107 proposes for the MCP server —
accept a JSON string and unmarshal it. So the CLI does not have the #97 bug, and
multi-leg via CLI does work.

Reasons I would still not make it the primary order path:

1. **It is `v0.0.14`, published roughly three hours ago.** The multi-leg path in it has
   had essentially no field exposure. For a one-week hackathon whose central risk is the
   order path, that is the wrong place to take a dependency on brand-new code.
2. **Alpaca's own skill calls the CLI "Alpha Preview"** and warns "Commands, flags, and
   output formats may change between releases, which is why your agent discovers flags at
   runtime rather than trusting any list in this file." Their own guidance is to not trust
   a documented flag list — including, transitively, this one. Note this release
   *regenerated the entire command surface from the OAS*, which is exactly the kind of
   change that moves flags.
3. **Subprocess + text parsing vs typed objects.** alpaca-py gives us `Order` models,
   typed enums, and client-side validation that catches the 2–4 leg rule and the
   unique-symbols rule *before* a network call. The CLI gives us stdout to parse and
   errors as text.
4. **We are already committed to alpaca-py** for option chains and contracts (§1.4–1.5) —
   the CLI does not remove that dependency. Using the SDK for orders too means one auth
   path, one error model, one set of types.

Where the CLI *is* clearly the right tool, and we should use it:

- **The backtest skill hard-requires it** for market-data fetch — that is not optional.
- **`--dry-run`** as a cheap preview/validation gate.
- Quick manual verification and debugging from the terminal during the build.

So: **CLI for backtest data and dry-run previews; alpaca-py for the live order path.**
The CLI is now a solid *fallback* for multi-leg if we hit an SDK problem, which is a real
improvement in our risk position — but it is a fallback, not the primary.

> **Cross-check against `hackathon-brief.md` (updated after this recon was written).**
> The brief lists as a non-negotiable requirement: *"Use either Alpaca's MCP server or
> Alpaca CLI."* alpaca-py alone does **not** satisfy that rule. The split above still
> complies — the CLI is genuinely used for backtest market-data fetch and `--dry-run`
> previews, and the `agentic` MCP plugin for account/data tools — but the CLI/MCP usage
> must be **real and demonstrable in the submission**, not incidental. If judges read the
> requirement strictly as "the *order path* must go through MCP or CLI", then CLI
> `--legs` (§4) becomes mandatory rather than a fallback, and the case for it flips.
> **Worth confirming in Discord alongside the P&L-window question.** Verified upside:
> CLI `--legs` works, so that reading is survivable at no research cost.
>
> The brief also confirms **Basic-plan market data permanently** — indicative option
> quotes, no OPRA. That matches the `OptionDataStream` default noted in §1.5
> (`feed=OptionsFeed.INDICATIVE`): we are stuck on the default, and `OptionsFeed.OPRA`
> is simply unavailable to us. Plan the strategy around indicative quotes and
> 15-minute-delayed option trades.

---

## 5. Official hackathon starter template — NONE EXISTS

**There is no official Alpaca hackathon starter template or boilerplate repo.**

I listed all 35 most-recently-pushed repos in the `alpacahq` org and searched GitHub for
hackathon repos. alpacahq has published nothing of the kind.

**UNKNOWN:** the lablab.ai hackathon page
(<https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon>) returns
**HTTP 403** to both WebFetch and a browser-UA curl, so its linked resources list could
not be read. Someone should open it in a real browser. Reported details from search
results only (unverified): online, 2026-08-28 → 2026-09-04, $6,000 prize pool.

### Closest official assets

**`alpacahq/agentic`** (pushed 2026-08-22) — <https://github.com/alpacahq/agentic> —
"Multi-platform plugin marketplace for Alpaca APIs". **This is the most useful find of the
recon and it is not in the original brief.** Hosted, OAuth-protected remote MCP servers
plus a Claude Code plugin marketplace. README: "No API keys to copy around — sign in with
your Alpaca account on first use."

Claude Code install, verbatim:

```bash
claude plugin marketplace add alpacahq/agentic
```

Then: start a Claude Code session, run `/plugin`, select the `alpaca-plugins`
marketplace, install `alpaca-trading` (and/or `alpaca-broker`), run `/reload-plugins`,
then `/mcp` to start a server and complete its OAuth flow.

| Plugin | Bundled MCP servers |
| --- | --- |
| `alpaca-trading` | Trading API (live), Trading API (paper) |
| `alpaca-broker` | Broker API (live), Broker API (sandbox) |

| MCP server | Endpoint |
| --- | --- |
| `alpaca-trading` | `https://api.alpaca.markets/mcp` |
| `alpaca-trading-paper` | `https://paper-api.alpaca.markets/mcp` |
| `alpaca-broker` | `https://broker-api.alpaca.markets/mcp` |
| `alpaca-broker-sandbox` | `https://broker-api.sandbox.alpaca.markets/mcp` |

Both Trading MCP servers expose Market Data API tools alongside trading tools, so one
connection covers account, order, position, portfolio and market data. README explicitly
names Claude Code as supported (alongside Cursor and Codex).

Repo contents: `.claude-plugin/marketplace.json`, `.cursor-plugin/marketplace.json`,
`.agents/plugins/marketplace.json`, and `plugins/{alpaca-trading,alpaca-broker}/` each
with `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
`.cursor-plugin/plugin.json`, `assets/logo.svg`.

> **UNKNOWN — worth a five-minute test early:** whether the *hosted* remote MCP shares the
> `legs` bug. It is a separate service from the `alpaca-mcp-server` codebase and I could
> not inspect it without completing OAuth. If the hosted server handles `legs` correctly
> it is strictly better than the local server for our purposes.

**Other official example code:**

- `alpacahq/gamma-scalping` — "Runnable algo template for gamma scalping options trading
  strategy" (last push 2025-07-18, stale)
- `alpacahq/options-wheel` — "Runnable algo template for trading the Options Wheel
  strategy" (last push 2025-06-05, stale)
- `alpacahq/notebooks` — Jupyter notebooks for getting started
- `alpacahq/alpaca-docs`, `alpacahq/alpaca-postman`

### Competitive landscape (incidental finding)

GitHub search for hackathon repos returned 16, of which ~15 were created in the last
48 hours. The overwhelming majority are **defined-risk / risk-gated options agents**:
`Sebastian0890/onenode-options-agent`, `ajalcance/glassbox-ai-quant`,
`Theodore-Liu/gated-agent`, `XnOwOCodes/strikewatch`, `mamartih/honest-wheel`,
`Joticle/Uncharted-Labs`, `guntoken/alpaca-wheel-agent`, `YSMsimon/glaz-trading-agent`,
plus two forks of `newsflow-trader` (news-driven LLM trading). It is a crowded field and
"autonomous options agent that manages its own risk" is close to the median entry.

---

## 6. Recommendations

1. **Pin Python 3.14** — verified working end-to-end with alpaca-py 0.44.0 and
   alpaca-mcp-server 2.3.0. Add an **explicit `pandas` pin** alongside it; alpaca-py's
   `pandas>=1.5.3` floats onto an untested pandas 3.x major.
2. **Try the `alpacahq/agentic` Claude Code plugin first** (`claude plugin marketplace add
   alpacahq/agentic`) — OAuth, no key handling, officially supported for Claude Code.
   Fall back to local `uvx alpaca-mcp-server` with `ALPACA_TOOLSETS` trimmed to only what
   we need, to dodge the known context-bloat issue (#45).
3. **Submit multi-leg option orders through alpaca-py directly.** #97 is unfixed and #107
   is unmerged on the local MCP server. CLI `--legs` (v0.0.14) is a verified working
   fallback if the SDK gives us trouble.
4. **Install `alpaca-trading-paper-trading`** (the generic variant) into `~/.claude/skills/`,
   not the `-mcp` or `-cli` variants, since we'll be driving alpaca-py from our own code.
   Add `alpaca-trading-backtest` too, but note it hard-requires the Alpha-preview CLI.
5. **Install the Alpaca CLI regardless** — the backtest skill requires it, and `--dry-run`
   is a cheap preview gate.
6. **Early spike (5 min):** test whether the hosted paper MCP endpoint handles a multi-leg
   `legs` array. If yes, it simplifies the order path considerably.

---

## Verification notes

- **Empirically tested locally:** alpaca-py 0.44.0 install + import + `mleg` request
  construction + `BarSet.df` on CPython 3.14.4; alpaca-mcp-server 2.3.0 install + import
  on 3.14.4.
- **Read from source at a pinned ref:** `alpaca-mcp-server` `main` `overrides.py`;
  `alpaca-py` `master` `requests.py` and `ci.yaml`; `alpacahq/cli` **tag `v0.0.14`**
  `commands.gen.go`, `trading_types.gen.go`, `order.go`.
- **Read via GitHub API:** repo trees, issue/PR states, release metadata, org repo list.
- **UNKNOWN / could not verify:** lablab.ai hackathon page contents (HTTP 403); whether
  the hosted remote MCP servers share the `legs` bug (requires OAuth); Python 3.12
  runtime behaviour (declared and classifier-listed, but not tested by me or by upstream CI).
