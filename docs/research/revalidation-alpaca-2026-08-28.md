# Alpaca Platform Revalidation — 2026-08-28

Revalidation of `docs/research/alpaca-platform.md` (captured 2026-08-26) ahead of the hackathon build.

**Method note.** Where the docs site and the API specification disagree, this document trusts the
specification. The Alpaca CLI README states that the generated binary is the source of truth for
commands, flags, enums, and validation, so findings below were verified against the artifacts
vendored in the `alpacahq/cli` repository rather than against docs prose alone:

- `api/specs/trading-api.json` (OpenAPI, trading)
- `api/specs/market-data-api.json` (OpenAPI, market data)
- `internal/cmd/commands.gen.go` (generated command bindings)
- `internal/cmd/testdata/command_tree.golden` (full command + flag tree)
- `internal/cmd/testdata/ops.golden` (flag → OAS param mapping)
- `internal/cmd/factory.go`, `internal/cmd/order.go`, `internal/cmdutil/flags.go`

Anything not established by a primary source is marked **UNKNOWN** and has deliberately not been
reconstructed from memory.

---

## Corrections to the Aug 26 dossier

Amend `docs/research/alpaca-platform.md` with each of these.

### C1. CLI version — v0.0.13 → **v0.0.14**

v0.0.14 was published **2026-08-28T16:23:08Z**, i.e. the morning of the build. The Aug 26 dossier
captured v0.0.13 (2026-07-22T18:56:08Z). The v0.0.14 changelog is:

```
* 53606273aa230a40c64b783425dcb3f4423ede30 Regenerate CLI from latest OAS (#13)
* f5f70832b52ef0c14bf41a0765270f355de1c126 fix: standardize User-Agent header across API client, OAuth, and update checks (#12)
```

Because the release regenerates the CLI from the OpenAPI spec, the command and flag surface moves
with it. Pin nothing to v0.0.13 behavior.

### C2. Multi-leg orders via CLI — "not supported, use the escape hatch" → **natively supported**

This is the most consequential correction. `alpaca order submit` has a `--legs` flag that is
JSON-decoded directly into the typed request body. The escape hatch is not required for spreads.
Full detail in item 3 below.

### C3. Four documented URLs now 404 — docs site moved to a `/us/` prefix

| Dead URL (in Aug 26 dossier) | Correct URL |
|---|---|
| `https://docs.alpaca.markets/docs/trading/options/` | `https://docs.alpaca.markets/us/docs/options-trading` |
| `https://docs.alpaca.markets/docs/options-trading-levels/` | `https://docs.alpaca.markets/docs/options-level-3-trading` (levels are documented here and in the OAS `Account` schema; there is no standalone "options-trading-levels" page any more) |
| `https://docs.alpaca.markets/docs/options-trading/multi-leg-options/` | `https://docs.alpaca.markets/docs/options-level-3-trading` |
| `https://docs.alpaca.markets/docs/trading/paper-trading/` | `https://docs.alpaca.markets/us/docs/paper-trading` |

Still live and correct: `https://docs.alpaca.markets/us/docs/alpacas-cli`,
`https://github.com/alpacahq/cli`, `https://docs.alpaca.markets/reference/get-options-contracts`,
`https://docs.alpaca.markets/docs/option-data/` (redirects into the option data section).

Additional useful URLs discovered: `https://docs.alpaca.markets/us/reference/postorder`,
`https://docs.alpaca.markets/us/docs/about-market-data-api`,
`https://docs.alpaca.markets/docs/real-time-option-data`.

### C4. Environment variable list was incomplete — 6 documented, **10 actually exist**

`ALPACA_CONFIG_DIR`, `ALPACA_QUIET`, `ALPACA_VERBOSE`, `ALPACA_DEBUG`, `ALPACA_TRACE` were missing.

### C5. Time-in-force for options — dossier said `day` or `gtc`; **spec says `day` only**

Unresolved contradiction between docs prose and the OpenAPI spec. Code against `day`. See item 6.

### C6. New finding — official agent skill ships in the CLI repo

`.agents/skills/alpaca-cli/SKILL.md` (10,384 bytes) is an Anthropic-format skill maintained by
Alpaca. Prefer it over hand-written CLI usage guidance for the agent build. A second skill,
`.agents/skills/alpaca-cli-regenerate/SKILL.md` (7,929 bytes), covers spec regeneration and is not
relevant to us.

### C7. New finding — prebuilt Linux binaries exist

The README documents only `go install` and Homebrew, but the GitHub releases carry prebuilt
tarballs. See item 1.

---

## 1. CLI install and version

**Status: CHANGED (version).**

### Documented install commands (verbatim from README)

```bash
# Go
go install github.com/alpacahq/cli/cmd/alpaca@latest

# Homebrew
brew install alpacahq/tap/cli
```

Homebrew works on Linux. If `go install` is used, `$GOPATH/bin` (usually `~/go/bin`) must be on
`PATH` or the binary will not resolve.

### Prebuilt tarball (undocumented but present in release assets)

Fastest path on a machine without a Go toolchain:

```bash
curl -fsSL -o /tmp/alpaca.tar.gz \
  https://github.com/alpacahq/cli/releases/download/v0.0.14/cli_0.0.14_linux_amd64.tar.gz
tar -xzf /tmp/alpaca.tar.gz -C /usr/local/bin alpaca
alpaca version
```

Full v0.0.14 asset list: `checksums.txt`, `cli_0.0.14_darwin_amd64.tar.gz`,
`cli_0.0.14_darwin_arm64.tar.gz`, `cli_0.0.14_linux_amd64.tar.gz`,
`cli_0.0.14_linux_arm64.tar.gz`, `cli_0.0.14_windows_amd64.zip`,
`cli_0.0.14_windows_arm64.zip`. Checksums:
`https://github.com/alpacahq/cli/releases/download/v0.0.14/checksums.txt`.

The exact layout inside the tarball (whether the binary sits at the root or under a directory) was
**not verified** — the archive was not downloaded and extracted. Adjust the `tar` invocation if the
above fails.

### Version

**v0.0.14**, published 2026-08-28T16:23:08Z, `target_commitish: main`, not a prerelease.

Release history: v0.0.14 (2026-08-28), v0.0.13 (2026-07-22), v0.0.12 (2026-06-22),
v0.0.11 (2026-05-22), v0.0.10 (2026-05-01). Tags run v0.0.1 through v0.0.14. Repository created
2026-02-26, description "CLI for Trading API", license Apache 2.0, not archived.

### Status: still Alpha Preview — CONFIRMED

Verbatim from the README:

> **Alpha Preview** - This CLI is under active development. Commands, flags, and output formats may
> change or be removed without notice between releases. Do not depend on current behavior in
> production workflows.

Also verbatim, and relevant to an autonomous agent build:

> Alpaca CLI is designed for AI agents, scripts, and automation pipelines. It is not an interactive
> trading terminal: there are no confirmation prompts, "are you sure?" dialogs, or interactive
> guardrails. Every command executes immediately.

Named destructive commands: `alpaca position close-all` liquidates the entire portfolio;
`alpaca order cancel-all` cancels every open order without listing them first; `alpaca locate create`
requests shares for a short sale and may incur locate fees.

### Self-update

```bash
alpaca update           # check, prompt, then upgrade
alpaca update --yes     # check and upgrade without prompting
alpaca update --check   # machine-readable JSON, no prompt
```

`--check` emits, e.g.:

```json
{"current":"0.0.1","latest":"0.0.2","update_available":true,"install_method":"goinstall","update_command":"go install github.com/alpacahq/cli/cmd/alpaca@latest"}
```

---

## 2. Environment variables

**Status: CONFIRMED, list extended (see C4).**

| Variable | Description (verbatim from README) |
|---|---|
| `ALPACA_API_KEY` | API key. Must be set with `ALPACA_SECRET_KEY`. |
| `ALPACA_SECRET_KEY` | Secret key. Must be set with `ALPACA_API_KEY`. |
| `ALPACA_LIVE_TRADE` | `true` routes to live trading. Anything else routes to paper trading. |
| `ALPACA_PROFILE` | Profile name to use. |
| `ALPACA_OUTPUT` | Default output format: `json` or `csv`. |
| `ALPACA_CONFIG_DIR` | Config directory. Defaults to `~/.config/alpaca`. |
| `ALPACA_QUIET` | Suppress non-data output such as warnings, hints, and color. |
| `ALPACA_VERBOSE` | Show HTTP request summaries on stderr. |
| `ALPACA_DEBUG` | Show HTTP request and response headers and bodies on stderr. |
| `ALPACA_TRACE` | Show HTTP timing breakdown on stderr. |

### `ALPACA_PAPER` is not a real setting — CONFIRMED

It appears nowhere in the README, the bundled agent skill, or the configuration table. Paper is the
default and `ALPACA_LIVE_TRADE=true` is the only opt-in. From the bundled skill, verbatim:

> Env-sourced credentials ignore the profile's `live_trade` field and default to paper unless
> `ALPACA_LIVE_TRADE=true` opts into live. Agents should set `ALPACA_LIVE_TRADE=true` explicitly
> when live trading is intended; any other value (including `false`) keeps you on paper.

### Credential precedence

Credentials resolve as an **atomic bundle** — no field-level mixing. First complete source wins:

1. `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` together
2. Profile `access_token` (OAuth)
3. Profile `api_key` + `secret_key`

A partial env bundle (only one of the two) falls through to the profile. Env API keys always beat
anything in a profile. OAuth tokens cannot be supplied via environment variable.

Paper vs live resolves **independently**: `ALPACA_LIVE_TRADE` > profile `live_trade` > paper default.

### Profiles

```bash
alpaca profile login                              # OAuth, paper trading
alpaca profile login --api-key                    # API key and secret, paper trading
alpaca profile login --api-key --live             # API key and secret, live trading
alpaca profile login --api-key --name prod --live # Named live profile
alpaca profile switch prod                        # Switch active profile
```

OAuth is paper-only; live requires API keys. Credentials are stored in
`~/.config/alpaca/profiles/` with 0600 permissions.

### Output and exit codes

JSON on stdout by default. Global flags: `--csv`, `--jq`, `--profile`, `--verbose`, `--debug`,
`--trace`, `--quiet`, `--schema`, `--timeout`. `--jq` applies a jq expression without an external
jq install. Errors are JSON on stderr:

```json
{"error":"rate limited","code":0,"status":429,"hint":"Rate limited. Reduce request frequency or add delays between calls."}
```

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | API or general error |
| `2` | Authentication error (401) |

Operational commands (`version`, `doctor`, `profile`, `update`, `completion`, help) emit
human-readable text; the exception is `alpaca update --check`, which emits JSON.

The CLI retries 429 and 5xx with exponential backoff up to 3 attempts, respecting `Retry-After`.

---

## 3. Multi-leg option orders via the CLI

**Status: CHANGED — natively supported. The Aug 26 conclusion was wrong.**

### Evidence

`internal/cmd/testdata/command_tree.golden` lists `--legs (string)` on `alpaca order submit`, and
`internal/cmd/testdata/ops.golden` maps it: `legs (body, string) -> legs`.

`internal/cmd/commands.gen.go:541` decodes it into the typed body:

```go
if cmdutil.Changed(cmd, "legs") {
    if err := json.Unmarshal([]byte(cmdutil.Str(cmd, "legs")), &body.Legs); err != nil {
        return nil, fmt.Errorf("--legs: %w", err)
    }
}
```

`body.Legs` is `[]MLegOrderLeg` — `internal/api/trading_types.gen.go:338`:

```go
Legs []MLegOrderLeg `json:"legs,omitempty"`
```

So `--legs` takes a JSON array string and is validated against the generated leg type.

### Complete flag set for `alpaca order submit`

```
--advanced-instructions (string)
--client-order-id (string)
--dry-run (bool)
--extended-hours (bool)
--legs (string)
--limit-price (string)
--notional (string)
--order-class (string)
--position-intent (string)
--qty (string)
--side (string)
--stop-loss (string)
--stop-price (string)
--symbol (string)
--take-profit (string)
--time-in-force (string)
--trail-percent (string)
--trail-price (string)
--type (string, default=market)
```

### Working invocation — two-leg debit call spread

```bash
alpaca order submit \
  --order-class mleg \
  --qty 1 \
  --type limit \
  --limit-price 1.00 \
  --time-in-force day \
  --client-order-id "$(uuidgen)" \
  --legs '[
    {"symbol":"AAPL260918C00250000","ratio_qty":"1","side":"buy","position_intent":"buy_to_open"},
    {"symbol":"AAPL260918C00260000","ratio_qty":"1","side":"sell","position_intent":"sell_to_open"}
  ]' \
  --quiet
```

Two traps:

1. **Omit `--symbol`.** The OAS says `symbol` is "required for all order classes except for `mleg`".
2. **Set `--type limit` explicitly.** It defaults to `market`; a market-order spread is almost
   certainly not what you want.

`--dry-run` prints the request body without submitting — worth running once to confirm `--legs`
serializes as expected. Implemented in `internal/cmd/order.go`:

```go
func configureOrderSubmit(cmd *cobra.Command) {
    cmd.Flags().Bool("dry-run", false, "Print the request body without submitting")
}
```

Note that `postOrderHook` in the same file defaults time-in-force when unset:

```go
func defaultTimeInForce(symbol string) api.TimeInForce {
    if strings.Contains(symbol, "/") {
        return "gtc"
    }
    return "day"
}
```

For an mleg order `body.Symbol` is empty, so this yields `day` — the correct value.

`ExtendedHours` is `bool` with `json:"extended_hours,omitempty"` (`trading_types.gen.go:337`), so
leaving `--extended-hours` unset omits the field entirely rather than sending `false`.

### Escape hatch — also confirmed

```bash
alpaca api GET /v2/account

echo '{"symbol":"AAPL","qty":"1","side":"buy","type":"market","time_in_force":"day"}' \
  | alpaca api POST /v2/orders
```

Syntax is `alpaca api [METHOD] <path>`; POST and PATCH bodies are piped on stdin.

### Idempotency

Always pass `--client-order-id` (max 128 chars) in automation. The API rejects duplicates with 409.
On an ambiguous failure, check before retrying:

```bash
alpaca order get-by-client-id --client-order-id "$CLIENT_ORDER_ID" --quiet
```

If the order comes back it went through — do not resubmit. A 404 (exit code 1) means retry is safe.

---

## 4. `alpaca doctor`

**Status: CONFIRMED (exists); check list UNKNOWN.**

Implemented at `internal/cmd/doctor.go` (4,346 bytes) with tests at `internal/cmd/doctor_test.go`.
Documented as "Check config and API connectivity" (README) and "show active profile + connectivity"
(bundled skill). The precise set of checks and their output format were **not verified** — the source
file was not read line by line. Run `alpaca doctor` once installed to see the actual output.

Related diagnostics:

```bash
alpaca account get --verbose   # request summary on stderr
alpaca account get --trace     # DNS, TLS, TTFB, total timing on stderr
alpaca account get --debug     # headers and bodies on stderr
```

Credentials are always scrubbed from diagnostic output.

### Command discovery

```bash
alpaca --help-all              # full command reference
alpaca order submit --help     # flags for one command
alpaca order submit --schema   # response fields without an API call
```

---

## 5. Options trading levels and account fields

**Status: CONFIRMED — field names verified in the OpenAPI `Account` and `AccountConfigurations`
schemas.**

Level 3 is required for spreads and verticals.

Level semantics, verbatim from the OAS: `0=disabled, 1=Covered Call/Cash-Secured Put,
2=Long Call/Put, 3=Spreads/Straddles.`

### Exact field names

All three are `integer` with `enum: [0, 1, 2, 3]`.

**On the `Account` object:**

- **`options_approved_level`** — *"The options trading level that was approved for this account."*
- **`options_trading_level`** — *"The effective options trading level of the account. This is the
  minimum between account `options_approved_level` and account configurations
  `max_options_trading_level`."*
- **`options_buying_power`** — `string`, *"Your buying power for options trading"*

**On the `AccountConfigurations` object:**

- **`max_options_trading_level`** — *"The desired maximum options trading level. 0=disabled,
  1=Covered Call/Cash-Secured Put, 2=Long Call/Put, 3=Spreads/Straddles."*

### The relationship, stated precisely

```
options_trading_level = min(options_approved_level, max_options_trading_level)
```

`max_options_trading_level` can be **downgraded** via `PATCH /v2/account/configurations`. Upgrades
require a separate API on live accounts.

### Reading it

```bash
alpaca account get --jq '{approved: .options_approved_level, effective: .options_trading_level, obp: .options_buying_power}' --quiet
```

The CLI exposes the config setter as `alpaca account config set --max-options-trading-level (int)`
(confirmed in `command_tree.golden`; `commands.gen.go:435` binds it through
`api.PatchAccountConfigOp` and only sends fields whose flags changed).

**Gate on `options_trading_level`, never `options_approved_level`.** An account can show
`options_approved_level: 3` while the effective level is lower because the configuration cap is
lower — orders will reject with a level error that looks, misleadingly, like an approval problem.

---

## 6. Multi-leg constraints

| Constraint | Status | Evidence |
|---|---|---|
| Max 4 legs | **CONFIRMED** | OAS `legs`: `"maxItems": 4`, description *"list of order legs (<= 4)"* |
| Min 2 legs | **CONFIRMED (secondary)** | Docs/support prose: "at least 2 but no more than 4". The OAS declares **no `minItems`** |
| Whole-number qty | **CONFIRMED** | Options orders prohibit notional; qty must be a whole number |
| Notional prohibited | **CONFIRMED** | OAS `notional`: *"Cannot work with `qty`. Can only work for market order types and day for time in force."* — and options docs prohibit notional outright |
| `market` / `limit` only | **CONFIRMED** | OAS `OrderType`: *"Multileg Options trading: market, limit."* |
| Time in force `day` / `gtc` | **CONFLICT — use `day`** | See below |
| No extended hours | **CONFIRMED** | Must be false or omitted |
| Explicit `position_intent` per leg | **CONFIRMED** | Required by docs; optional in schema but always send it |
| Ratios reduced by GCD | **CONFIRMED** | *"each leg's `leg_ratio` must be in its simplest form"*; GCD across ratios must equal 1. Example rejected: legs with `ratio_qty` 4 and 2 (GCD 2) |
| No equity legs | **CONFIRMED** | *"MLeg orders that include an equity leg are not supported at this time."* |
| No uncovered short legs | **CONFIRMED** | *"an MLeg order is accepted only if all its legs are covered within the same MLeg order"* — prevents multiple unhedged short calls in one order |
| All-or-nothing execution | **CONFIRMED (prose)** | Orders execute as unified units, all legs filling together or not at all |

### The time-in-force conflict

- The options-trading overview page states TIF is limited to **`day` or `gtc`**.
- The OpenAPI `TimeInForce` description states: *"Options trading: **day**."* — `gtc` is not listed
  for options.

These contradict each other and the contradiction was **not resolved**. Use `day`. If `gtc` is
needed, test it against paper explicitly before depending on it.

For reference, the full `TimeInForce` enum is `["day", "gtc", "opg", "cls", "ioc", "fok"]`, with the
per-asset breakdown: *"Equity trading: day, gtc, opg, cls, ioc, fok. Options trading: day. Crypto
trading: gtc, ioc."*

### `limit_price` sign convention for mleg — easy to get backwards

Verbatim from the OAS `limit_price` description:

> Required if type is `limit` or `stop_limit`.
> In case of `mleg`, the limit_price parameter is expressed with the following notation:
> - A positive value indicates a debit, representing a cost or payment to be made.
> - A negative value signifies a credit, reflecting an amount to be received.

### `qty` for mleg

Verbatim: *"number of shares to trade. Can be fractionable for only market and day order types.
Required for `mleg` order class, represents the number of units to trade of this strategy."*

So `qty` is the number of spreads, not the number of contracts. Contracts per leg =
`qty × ratio_qty`.

---

## 7. REST payload for a two-leg debit spread

**Status: CONFIRMED.**

Endpoint: `POST /v2/orders` (operationId `postOrder`).

### Relevant enums, verbatim from the OAS

**`OrderClass`** — `["simple", "bracket", "oco", "oto", "mleg", ""]`

> The order classes supported by Alpaca vary based on the order's security type. The following
> provides a comprehensive breakdown of the supported order classes for each category:
> - Equity trading: simple (or ""), oco, oto, bracket.
> - Options trading:
>   - simple (or "")
>   - mleg (required for multi-leg complex option strategies)
> - Crypto trading: simple (or "").

**`PositionIntent`** — `["buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"]`,
described as *"Represents the desired position strategy."*

**`OrderType`** — `["market", "limit", "stop", "stop_limit", "trailing_stop"]`, but
*"Multileg Options trading: market, limit."*

### `MLegOrderLeg` schema, verbatim

```json
{
 "description": "Represents an individual leg of a multi-leg options order.",
 "properties": {
  "position_intent": { "$ref": "#/components/schemas/PositionIntent" },
  "ratio_qty": {
   "description": "proportional quantity of this leg in relation to the overall multi-leg order qty",
   "type": "string"
  },
  "side": { "$ref": "#/components/schemas/OrderSide" },
  "symbol": {
   "description": "symbol or asset ID to identify the asset to trade",
   "type": "string"
  }
 },
 "required": ["symbol", "ratio_qty"],
 "title": "MLegOrderLeg",
 "type": "object"
}
```

Only `symbol` and `ratio_qty` are schema-required, but `side` and `position_intent` should always be
sent — the docs treat `position_intent` as required for multi-leg.

### Concrete payload — two-leg vertical debit call spread

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "1.00",
  "time_in_force": "day",
  "legs": [
    {
      "symbol": "AAPL250117C00190000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_open"
    },
    {
      "symbol": "AAPL250117C00210000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_open"
    }
  ]
}
```

Structural rules:

- **No top-level `symbol`** — the only order class where it is omitted.
- **No top-level `side`** and **no top-level `position_intent`** — both live on each leg.
- **All values are strings**, including `qty`, `ratio_qty`, and `limit_price`.
- Positive `limit_price` = debit (this example pays 1.00 per spread).
- For a **credit** spread (e.g. a short vertical), `limit_price` is negative.

### Unequal-ratio example, verbatim from the API reference

```json
{
  "legs": [
    {
      "position_intent": "buy_to_open",
      "ratio_qty": "3",
      "side": "buy",
      "symbol": "AAPL241213C00250000"
    },
    {
      "position_intent": "sell_to_open",
      "ratio_qty": "1",
      "side": "sell",
      "symbol": "AAPL241213C00260000"
    }
  ],
  "limit_price": "10",
  "order_class": "mleg",
  "qty": "3",
  "time_in_force": "day",
  "type": "limit"
}
```

Ratios 3 and 1 have GCD 1, so this passes the simplest-form rule.

### Closing a spread

Use the mirrored intents: `sell_to_close` on the long leg, `buy_to_close` on the short leg, with the
same `ratio_qty` values, and flip the `limit_price` sign (closing a debit spread is a credit).

---

## 8. `GET /v2/options/contracts` query parameters

**Status: CONFIRMED.**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `underlying_symbols` | string | — | One or more symbols, comma-separated |
| `status` | enum | `active` | `active`, `inactive` |
| `expiration_date` | date `YYYY-MM-DD` | — | Exact expiration |
| `expiration_date_gte` | date `YYYY-MM-DD` | — | On or after |
| `expiration_date_lte` | date `YYYY-MM-DD` | **next weekend** | On or before |
| `root_symbol` | string | — | |
| `type` | enum | — | `call`, `put` |
| `style` | enum | — | `american`, `european` |
| `strike_price_gte` | number | — | |
| `strike_price_lte` | number | — | |
| `page_token` | string | — | Pagination cursor |
| `limit` | integer | `100` | Max 10,000 per page |
| `show_deliverables` | boolean | — | Include deliverables array |
| `ppind` | boolean | — | Penny Program Indicator |

### Default expiry window — the trap

Verbatim: *"By default this is set to the next weekend"* (`expiration_date_lte`), and
*"Only active contracts that expire before the upcoming weekend are returned"* without explicit
filters.

**Always pass explicit `expiration_date_gte` and `expiration_date_lte`.** Otherwise a chain lookup
silently returns only the current week's contracts, which looks like a thin or empty universe rather
than a filtering mistake.

### CLI equivalent

`alpaca option contracts`, with kebab-case mirrors of every parameter:

```
--expiration-date --expiration-date-gte --expiration-date-lte --limit --page-token
--ppind --root-symbol --show-deliverables --status --strike-price-gte --strike-price-lte
--style --type --underlying-symbols
```

Related: `alpaca option get --symbol-or-id`, `alpaca option exercise --symbol-or-contract-id`,
`alpaca option do-not-exercise --symbol-or-contract-id`.

---

## 9. Free "Basic" plan limits for options data

**Status: CONFIRMED.**

- **Feed: Indicative Pricing Feed only.** The official OPRA feed requires a paid subscription. The
  Basic plan is the default for both paper and live accounts.
- **15-minute delay: still accurate.** The OAS states it twice, as the default behavior for users
  without real-time access. `start`: *"Default: the beginning of the current day, but at least 15
  minutes ago if the user doesn't have real-time access for the feed."* `end`: *"Default: the current
  time if the user has a real-time access for the feed, otherwise 15 minutes before the current
  time."*
- **Quotes are indicative — and the wording matters.** From the OAS `feed` parameter description:
  *"`opra` is the official OPRA feed, `indicative` is a free indicative feed where trades are
  **delayed** and quotes are **modified**."* Quotes are altered, not merely delayed. Do not treat a
  free-plan quote as the true NBBO.
- **REST rate limit: 200 API calls/min** on Basic.
- **WebSocket subscription limit: 200 option quotes** on Basic.
- **Wildcard `*` subscription is prohibited for option quotes** — *"there are simply too many of
  them"*.
- **The options stream is msgpack-only**, unlike the stock and crypto streams. Requesting JSON
  returns **error code 412**.
- **Historical option data starts February 2024** — *"we only offer historical option data since
  February 2024"*.
- Equities on Basic, for contrast: IEX only, 30-symbol WebSocket cap, history since 2016, same
  200/min.

Paid comparison: **Algo Trader Plus, $99/month** — full OPRA feed, no 15-minute restriction,
10,000 API calls/min, unlimited equity WebSocket subscriptions.

### Feed defaulting — important subtlety

The CLI shows `--feed (string, default=opra)` on `alpaca data option chain`, `snapshot`,
`latest-quotes`, and `latest-trades`. This looks like it would fail on a Basic plan. **It does not.**
`queryFromFlags` in `internal/cmd/factory.go:69` only emits query parameters for flags that were
explicitly changed:

```go
func queryFromFlags(cmd *cobra.Command, op api.Op) url.Values {
    v := url.Values{}
    for _, f := range op.Flags {
        if f.Source != "query" || !cmd.Flags().Changed(f.Name) {
            continue
        }
        ...
    }
    return v
}
```

So the displayed default is never transmitted. The server then applies its own rule, verbatim:
*"Default: `opra` if the user has a subscription, otherwise `indicative`."*

**Omit `--feed` entirely and the right feed is selected automatically.** Explicitly passing
`--feed opra` on a Basic plan is what breaks.

### Hackathon-specific data upgrade — UNKNOWN

No participant data upgrade is documented anywhere found. Free Algo Trader Plus appears in the docs
only as an **Alpaca Elite** account benefit ("free Algo Trader Plus market data subscription on us",
personal use only). There is an active *Alpaca AI Trading Agents Hackathon* listed on lablab.ai
ending **Sep 4, 2026**, explicitly about building agents on Alpaca's Trading API, MCP server and
CLI — its own page may list perks, but Alpaca's documentation promises nothing. **Plan for Basic
limits.**

---

## 10. Greeks and implied volatility

**Status: field names CONFIRMED; absence conditions UNKNOWN.**

Note the **camelCase**, which is inconsistent with the snake_case used elsewhere in the API.

### `option_greeks` schema, verbatim

```json
{
 "description": "The greeks for the contract calculated using the Black-Scholes model.",
 "properties": {
  "delta": { "format": "double", "type": "number" },
  "gamma": { "format": "double", "type": "number" },
  "rho":   { "format": "double", "type": "number" },
  "theta": { "format": "double", "type": "number" },
  "vega":  { "format": "double", "type": "number" }
 },
 "required": ["delta", "gamma", "theta", "vega", "rho"],
 "type": "object"
}
```

### `option_snapshot` schema, verbatim

```json
{
 "description": "A snapshot provides the latest trade and latest quote.",
 "properties": {
  "dailyBar": { "$ref": "#/components/schemas/option_bar" },
  "greeks": { "$ref": "#/components/schemas/option_greeks" },
  "impliedVolatility": {
   "description": "Implied volatility calculated using the Black-Scholes model.",
   "format": "double",
   "type": "number"
  },
  "latestQuote": { "$ref": "#/components/schemas/option_quote" },
  "latestTrade": { "$ref": "#/components/schemas/option_trade" },
  "minuteBar": { "$ref": "#/components/schemas/option_bar" },
  "prevDailyBar": { "$ref": "#/components/schemas/option_bar" }
 },
 "type": "object"
}
```

So the carrying fields are **`greeks`** (with `delta`, `gamma`, `theta`, `vega`, `rho`) and
**`impliedVolatility`**, both on the snapshot object.

Returned by `GET /v1beta1/options/snapshots` (operationId `OptionSnapshots`) and
`GET /v1beta1/options/snapshots/{underlying_symbol}` (operationId `OptionChain`); CLI equivalents
`alpaca data option snapshot` and `alpaca data option chain`.

Response envelope `option_snapshots_resp` has `snapshots` (map keyed by contract symbol) and
`next_page_token`, both required.

### When they are absent — UNKNOWN

The `option_snapshot` schema has **no `required` list at all**, so every field including `greeks`
and `impliedVolatility` is formally optional and may be omitted per contract. **No documentation
found states the conditions under which they are dropped.** The commonly repeated explanations
(deep ITM/OTM, illiquid contracts, missing underlying price, expiry-day contracts) appear in no
primary source and are deliberately not asserted here.

**Practical guidance:** null-check `greeks` and `impliedVolatility` on every snapshot, and probe a
live chain early in the build to observe the real absence pattern empirically.

---

## 11. Paper trading fill simulation for multi-leg orders

**Status: UNKNOWN — genuine documentation gap.**

Correct URL: `https://docs.alpaca.markets/us/docs/paper-trading`.

### What is documented (equity-oriented)

- *"Orders are filled only when they become marketable"* — buy limit orders fill when the limit
  price meets or exceeds the best ask; sell limit orders fill when the limit price meets or falls
  below the best bid.
- *"When orders are eligible to be filled, they will receive partial fills for a random size 10% of
  the time."* If the remaining quantity is still marketable after a partial fill, it is re-evaluated.

### Documented limitations

Paper trading does **not** account for:

- Market impact of your orders
- Information leakage of your orders
- Price slippage due to latency
- Order queue position (for non-marketable limit orders)
- Price improvement received
- Regulatory fees
- Dividends

Additional constraints:

- Borrow fees are not simulated ("Coming Soon")
- *"Your order quantity is not checked against the NBBO quantities"* — fills can exceed real
  available liquidity
- No order fill email notifications
- Account balance cannot be modified after creation without resetting the account
- *"paper trading is only a simulation"* and may not reflect actual trading performance

### The gap

**Nothing in the paper trading documentation addresses options or multi-leg (mleg) orders.** The
entire fill model described is equity-shaped. The following were **not** established and should be
determined empirically before relying on paper results:

1. Whether the net debit/credit `limit_price` is evaluated against combined leg quotes, and how.
2. Whether the 10% random partial-fill rule applies to a multi-leg order — it would directly
   contradict the all-or-nothing framing in the level-3 documentation.
3. How fills behave when the **free indicative feed's modified quotes** drive the marketability
   check.

Point 3 is the substantive risk: on a Basic plan, paper multi-leg fills are simulated against quotes
the documentation itself describes as *modified*. **Treat paper fill prices as directionally useful
and numerically untrustworthy.**

---

## Implementation checklist

1. Install v0.0.14 today — v0.0.13 is a release behind and the surface is regenerated per release.
2. Use `--legs` natively; it gives typed validation and `--dry-run`. No escape hatch needed.
3. Omit `--symbol` and set `--type limit` explicitly on every mleg submit.
4. Always pass `expiration_date_gte` / `expiration_date_lte` on contract lookups.
5. Never pass `--feed`; let the server choose `indicative`.
6. Gate on `options_trading_level == 3`, not `options_approved_level`.
7. `limit_price` positive = debit, negative = credit, for mleg only.
8. Time in force `day`.
9. Null-check `greeks` and `impliedVolatility` on every snapshot.
10. Always pass `--client-order-id`; check `order get-by-client-id` before any retry.
11. Reduce leg ratios by GCD before submitting.
12. Budget against 200 API calls/min and a 200-symbol option quote WebSocket cap.

---

## Source URLs

- CLI docs — https://docs.alpaca.markets/us/docs/alpacas-cli
- CLI repository — https://github.com/alpacahq/cli
- CLI v0.0.14 release — https://github.com/alpacahq/cli/releases/tag/v0.0.14
- Linux amd64 tarball — https://github.com/alpacahq/cli/releases/download/v0.0.14/cli_0.0.14_linux_amd64.tar.gz
- Options Trading — https://docs.alpaca.markets/us/docs/options-trading
- Options Level 3 / multi-leg — https://docs.alpaca.markets/docs/options-level-3-trading
- Options Orders examples — https://docs.alpaca.markets/us/docs/options-orders
- Create an Order (reference) — https://docs.alpaca.markets/us/reference/postorder
- Get Option Contracts — https://docs.alpaca.markets/reference/get-options-contracts
- About Market Data API (plan limits) — https://docs.alpaca.markets/us/docs/about-market-data-api
- Real-time Option Data — https://docs.alpaca.markets/docs/real-time-option-data
- Historical Option Data — https://docs.alpaca.markets/docs/historical-option-data
- Paper Trading — https://docs.alpaca.markets/us/docs/paper-trading
- Hackathon listing — https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon

Local copies of the specs and generated metadata used for verification (session scratchpad, ephemeral):
`trading-api.json`, `md.json`, `tree.golden`, `ops.golden`, `commands.gen.go`, `ttypes.gen.go` under
`/tmp/claude-1000/-home-ianwalmsley-projects-alpaca/8645b884-c4d6-46b4-975e-929328a76b90/scratchpad/`.
