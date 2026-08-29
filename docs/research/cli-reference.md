# Alpaca CLI Reference (v0.0.14) — empirically verified

**Binary:** `/home/ianwalmsley/.local/bin/alpaca` (ELF 64-bit, statically linked, stripped, Go 1.24.0, linux/amd64)
**Version string:** `0.0.14`
**Verified:** 2026-08-28, against a **paper** account (`PA3ELCLN8HZS`), `ALPACA_LIVE_TRADE=false`.
**Method:** every claim below marked "verified" was produced by running the local binary. Everything the CLI itself
could not tell us (i.e. anything that requires the API to actually accept a `POST /v2/orders`) is marked
**UNVERIFIED** or **UNKNOWN** — no real order was submitted.

Upstream prose docs: <https://docs.alpaca.markets/us/docs/alpacas-cli>. **The published docs are wrong in several
places** (see [§11 Doc errata](#11-doc-errata)). Trust this file and `alpaca --help-all` over the website.

---

## 0. TL;DR for callers shelling out

```bash
alpaca order submit \
  --order-class mleg \
  --qty 1 \
  --type limit \
  --limit-price 1.05 \
  --time-in-force day \
  --client-order-id uw-20260828-abc123 \
  --legs '[{"symbol":"SPY260918P00315000","ratio_qty":"1","side":"sell","position_intent":"sell_to_open"},{"symbol":"SPY260918P00310000","ratio_qty":"1","side":"buy","position_intent":"buy_to_open"}]'
```

Add `--dry-run` to print the body without submitting. **Do NOT rely on `--dry-run` as a validator** — see §1.4.

Five things that will bite you:

1. `--dry-run` does **zero** validation. It is a JSON pretty-printer for the locally-assembled body. Every
   malformed order below printed happily and exited `0`.
2. `--type` defaults to **`market`**. Omit it on an mleg and you build a market multi-leg order.
3. `--csv` combined with `--jq` prints **nothing** and exits `0`. Silent data loss.
4. Exit code `2` means *HTTP 401 only*. Missing credentials entirely exits `1`. "2 = auth failure" is too coarse.
5. `alpaca doctor` prints `✓ active profile: paper` **even when `ALPACA_LIVE_TRADE=true` and the base URL is
   `https://api.alpaca.markets`**. That line is not a safety indicator. Check the `Trading:` URL instead.

---

## 1. `alpaca order submit`

```
Usage: alpaca order submit [flags]
```

### 1.1 Complete flag list (verbatim from `alpaca order submit --help`)

Every flag is `string` except `--dry-run` and `--extended-hours`, which are booleans.

| Flag | Type | Default | Help text (verbatim) |
|---|---|---|---|
| `--advanced-instructions` | string | — | advanced instructions for Elite Smart Router: https://docs.alpaca.markets/docs/alpaca-elite-smart-router |
| `--client-order-id` | string | auto-generated | A unique identifier for the order. Automatically generated if not sent. (<= 128 characters) |
| `--dry-run` | bool | false | Print the request body without submitting |
| `--extended-hours` | bool | false | (default) false |
| `--legs` | string (JSON) | — | list of order legs (<= 4) |
| `--limit-price` | string | — | required if type is limit or stop_limit. |
| `--notional` | string | — | dollar amount to trade. Cannot work with qty. Can only work for market order types and day for time in force |
| `--order-class` | string | — | order classes supported by Alpaca vary based on the order's security type |
| `--position-intent` | string | — | represents the desired position strategy |
| `--qty` | string | — | number of shares to trade |
| `--side` | string | — | represents which side this order was on: - buy - sell **Required for all order classes except for mleg** |
| `--stop-loss` | string (JSON) | — | takes in string/number values for stop_price and limit_price |
| `--stop-price` | string | — | required if type is stop or stop_limit |
| `--symbol` | string | — | symbol, asset ID, or currency pair to identify the asset to trade, **required for all order classes except for mleg** |
| `--take-profit` | string (JSON) | — | takes in a string/number value for limit_price |
| `--time-in-force` | string | `day` (injected) | time-In-Force values supported by Alpaca vary based on the order's security type |
| `--trail-percent` | string | — | this or trail_price is required if type is trailing_stop |
| `--trail-price` | string | — | this or trail_percent is required if type is trailing_stop |
| `--type` | string | **`market`** | order types supported by Alpaca vary based on the order's security type |

There is **no** `--live`, `--paper`, `--output`, or `--yes` flag on `order submit`:

```
$ alpaca order submit --live --symbol AAPL --qty 1 --side buy --type market --dry-run
{
  "code": 0,
  "error": "unknown flag: --live",
  "hint": "",
  "status": 0
}
exit 1
```

Note the doc-listed enum values (`--order-class` etc.) are not enforced client-side; they come from the response
schema (`alpaca order submit --schema`):

- `order_class`: `"bracket" | "mleg" | "oco" | "oto" | "simple"`
- `type`: `"limit" | "market" | "stop" | "stop_limit" | "trailing_stop"`
- `time_in_force`: `"cls" | "day" | "fok" | "gtc" | "ioc" | "opg"`
- `position_intent`: `"buy_to_close" | "buy_to_open" | "sell_to_close" | "sell_to_open"`
- `side`: `"buy" | "sell"`
- `qty` comment: *"Required if order class is mleg"*

### 1.2 What is required for an `mleg` order

From the CLI's own help + schema, and from Alpaca's options docs:

**Required:** `--order-class mleg`, `--qty`, `--legs`, `--type limit`, `--limit-price`, `--time-in-force day`.
**Must be omitted:** `--side` (help: "Required for all order classes except for mleg").
**Should be omitted:** `--symbol` (help: "required for all order classes except for mleg").

Caveat — the CLI enforces **none** of this. See §1.4.

### 1.3 Verified `--dry-run` output for a two-leg put credit spread

Real, currently-tradable SPY contracts (from `alpaca option contracts --underlying-symbols SPY --type put
--expiration-date-gte 2026-09-15 --expiration-date-lte 2026-09-19`): short `SPY260918P00315000`, long
`SPY260918P00310000`.

Command run:

```bash
alpaca order submit \
  --order-class mleg \
  --qty 1 \
  --type limit \
  --limit-price 1.00 \
  --time-in-force day \
  --legs '[{"symbol":"SPY260918P00315000","ratio_qty":"1","side":"sell","position_intent":"sell_to_open"},{"symbol":"SPY260918P00310000","ratio_qty":"1","side":"buy","position_intent":"buy_to_open"}]' \
  --dry-run
```

**Exact printed body (stdout, exit 0, stderr empty):**

```json
{
  "advanced_instructions": {},
  "legs": [
    {
      "position_intent": "sell_to_open",
      "ratio_qty": "1",
      "side": "sell",
      "symbol": "SPY260918P00315000"
    },
    {
      "position_intent": "buy_to_open",
      "ratio_qty": "1",
      "side": "buy",
      "symbol": "SPY260918P00310000"
    }
  ],
  "limit_price": "1.00",
  "order_class": "mleg",
  "qty": "1",
  "time_in_force": "day",
  "type": "limit"
}
```

Observations on the body itself:

- Keys are emitted **alphabetically sorted**, and leg keys are sorted too. Don't string-compare against a
  hand-written body; parse it.
- `"advanced_instructions": {}` is **always present**, an empty object, even when `--advanced-instructions` was
  never passed. This goes on the wire on a real submit. Whether the API tolerates it on every order type is
  **UNVERIFIED** (it was clearly fine for the CLI's own default path, but we did not POST).
- Flags that were not supplied are simply absent from the body — except `time_in_force`, which the CLI injects
  as `"day"`, and `type`, injected as `"market"`.

With `--client-order-id uw-test-abc-123` the body gains exactly one key (verified):

```json
  "client_order_id": "uw-test-abc-123",
```

### 1.4 `--dry-run` does not validate anything — verified failures to reject

`--dry-run` assembles the body locally, prints it, and exits `0`. It makes **no HTTP request** (proved: it prints
the body normally with deliberately bogus credentials, which would otherwise 401). It does still require *some*
credentials to be present — with no `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` at all it fails the auth precheck:

```
{
  "code": 0,
  "error": "authentication required\nHint: run `alpaca profile login` to authenticate",
  "hint": "",
  "status": 0
}
exit 1
```

All of the following were **accepted, printed, exit 0**:

| Case | What happened |
|---|---|
| `--type` omitted on an mleg | body contains `"type": "market"` — a **market multi-leg order** |
| `--symbol SPY` passed alongside `--legs` | body contains **both** `"symbol": "SPY"` and `"legs": [...]` — not rejected, not stripped |
| `--order-class` omitted but `--legs` supplied | body has `legs` and **no** `order_class` key |
| 5 legs (help says `<= 4`) | all 5 emitted, no error |
| `--legs '[]'` | **the `legs` key is omitted entirely** — you get a bare `order_class: mleg` order with no legs |
| `--legs` omitted entirely with `--order-class mleg` | same: no `legs` key, exit 0 |
| `--qty` omitted on an mleg | no `qty` key; schema says qty is required for mleg |
| `"side":"SELL"` (wrong case) | passed through verbatim as `"SELL"` |
| unknown leg field `"bogus_field":"x"` | **silently dropped** from the body, no warning |
| `--type limit` with no `--limit-price` (simple equity order) | no `limit_price` key, exit 0 |

Practical consequence for calling code: build and validate the payload yourself. Use `--dry-run` only to confirm
that what you *intended* is what the CLI *serialised* — diff the parsed JSON against your expectation. Treat exit
0 from `--dry-run` as "the CLI parsed my flags", never as "Alpaca will accept this".

Whether the API rejects `type: market` mleg, a stray `symbol` on an mleg, or a 5-leg order is **UNVERIFIED** —
confirming would require a live POST, which was out of scope. Alpaca's own options docs only ever demonstrate
`type: "limit"` + `time_in_force: "day"` for mleg, so treat `--type limit` as mandatory.

### 1.5 `--legs` JSON shape (verified)

`--legs` is a single string argument containing a **JSON array**. It is unmarshalled into Go type
`[]api.MLegOrderLeg`. Fields, all **strings**:

```json
[
  {
    "symbol":          "SPY260918P00315000",
    "ratio_qty":       "1",
    "side":            "sell",
    "position_intent": "sell_to_open"
  }
]
```

Only these four keys survive serialisation; anything else is dropped silently.

**Verified error messages for malformed input** (all on **stderr**, stdout empty, **exit 1**):

Not JSON at all — `--legs notjson`:

```json
{
  "code": 0,
  "error": "--legs: invalid character 'o' in literal null (expecting 'u')",
  "hint": "",
  "status": 0
}
```

A JSON object instead of an array — `--legs '{"symbol":"SPY260918P00315000"}'`:

```json
{
  "code": 0,
  "error": "--legs: json: cannot unmarshal object into Go value of type []api.MLegOrderLeg",
  "hint": "",
  "status": 0
}
```

Wrong scalar type — `--legs '[{"symbol":"...","ratio_qty":1,"side":"sell"}]'` (numeric `ratio_qty`):

```json
{
  "code": 0,
  "error": "--legs: json: cannot unmarshal number into Go struct field MLegOrderLeg.ratio_qty of type string",
  "hint": "",
  "status": 0
}
```

The Go type name `api.MLegOrderLeg` / `MLegOrderLeg.<field>` leaks into the error, which makes these messages
reliable to pattern-match on: `^--legs: ` prefixes every one.

### 1.6 Credit vs debit sign convention

**UNKNOWN.** `--limit-price` is passed through verbatim as a string (`"1.00"` above). Alpaca's docs describe the
mleg limit price as a "net price (debit/credit)" but do not state the sign convention explicitly, and we could not
test it without submitting. Resolve this before going live.

---

## 2. `alpaca order list` / `get` / `get-by-client-id` / `cancel` / `cancel-all` / `replace`

### 2.1 `alpaca order list`

```
Usage: alpaca order list [flags]
```

| Flag | Notes (verbatim help) |
|---|---|
| `--after` | response will include only ones submitted after this timestamp (exclusive.) |
| `--after-order-id` | return orders submitted after the order with this ID (exclusive). Mutually exclusive with before_order_id |
| `--asset-class` | A comma-separated list of asset classes |
| `--before-order-id` | return orders submitted before the order with this ID (exclusive). Mutually exclusive with after_order_id |
| `--direction` | asc or desc. Defaults to desc |
| `--limit` | maximum number of orders in response. Defaults to 50 and max is 500 |
| `--nested` | if true, the result will roll up multi-leg orders under the legs field of primary order |
| `--side` | filters down to orders that have a matching side field set |
| `--status` | open, closed or all. Defaults to **open** |
| `--symbols` | A comma-separated list of symbols to filter by |
| `--until` | response will include only ones submitted until this timestamp (exclusive.) |

**Output shape: a bare JSON array** (not an envelope). Verified on the empty paper account:

```
$ alpaca order list
[]
exit 0
```

`--status` defaults to `open`, so a plain `alpaca order list` will **not** show a filled or rejected order.
Use `--status all` when reconciling.

For mleg, pass `--nested` so the child legs are rolled up under `legs` on the parent order. The exact nested
payload is **UNVERIFIED** (account had zero orders).

**No client-side validation of filter values** — `--status bogus` and `--limit 501` both returned `[]` with exit
0 rather than an error. On an empty account we cannot distinguish "filter ignored" from "genuinely no orders";
treat filter typos as silently-wrong, not as errors.

Per-element schema (`alpaca order list --schema`, no API call needed) — the Order entity:

```
asset_class: "corporate" | "crypto" | "crypto_perp" | "global_equity" | "ipo" | "treasury" | "us_equity" | "us_equity_chain" | "us_index" | "us_option";
asset_id: string;             // For options this is the option contract ID
canceled_at: string;
client_order_id: string;
created_at: string;
expired_at: string;
expires_at: string;
extended_hours: boolean;
failed_at: string;
filled_at: string;
filled_avg_price: string;
filled_qty: string;
hwm: string;
id: string;                   // order ID
legs: object[];               // nested-style multi-leg children
limit_price: string;
notional: string;
order_class: "bracket" | "mleg" | "oco" | "oto" | "simple";
order_type: string;           // deprecated in favour of "type"
position_intent: "buy_to_close" | "buy_to_open" | "sell_to_close" | "sell_to_open";
qty: string;                  // Required if order class is mleg
ratio_qty: string;
replaced_at: string;
replaced_by: string;
replaces: string;
side: "buy" | "sell";
status: "accepted" | "accepted_for_bidding" | "calculated" | "canceled" | "done_for_day" | "expired" | "filled" | "held" | "new" | "partially_filled" | "pending_cancel" | "pending_new" | "pending_replace" | "rejected" | "replaced" | "stopped" | "suspended";
stop_price: string;
submitted_at: string;
symbol: string;
time_in_force: "cls" | "day" | "fok" | "gtc" | "ioc" | "opg";
trail_percent: string;
trail_price: string;
type: "limit" | "market" | "stop" | "stop_limit" | "trailing_stop";
updated_at: string;
```

### 2.2 `alpaca order get`

```
Usage: alpaca order get [flags]
  --nested     roll up multi-leg orders under the legs field of primary order
  --order-id   order id
```

Verified error, missing flag (stderr, exit 1):

```json
{
  "code": 0,
  "error": "--order-id required (see 'alpaca order get --help' for examples)",
  "hint": "",
  "status": 0
}
```

Verified error, unknown ID (stderr, exit 1):

```json
{
  "code": 40410000,
  "error": "order not found for 00000000-0000-0000-0000-000000000000",
  "hint": "",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/orders/00000000-0000-0000-0000-000000000000",
  "request_id": "fc793dbec9a709b5257ff69a4bfadff0",
  "status": 404
}
```

Verified error, malformed ID (stderr, exit 1) — note the API, not the CLI, catches this:

```json
{
  "code": 40010001,
  "error": "order_id is missing",
  "hint": "Validation error. Check parameter values — common issues: invalid qty, bad price, unknown symbol, or missing required fields.",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/orders/not-a-uuid",
  "request_id": "819c61843fe03f021d4ce2591de0b370",
  "status": 422
}
```

### 2.3 Lookup by `client_order_id` — the idempotency path

**This is the command you want after a timeout.** It is a separate subcommand, not a filter on `order list`:

```bash
alpaca order get-by-client-id --client-order-id <id>
```

It hits `GET /v2/orders:by_client_order_id?client_order_id=<id>`. `alpaca order list` has **no**
`--client-order-id` filter — do not try to reconcile through `list`.

Verified miss (stderr, exit 1):

```json
{
  "code": 40410000,
  "error": "order not found for definitely-does-not-exist-12345",
  "hint": "",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/orders:by_client_order_id?client_order_id=definitely-does-not-exist-12345",
  "request_id": "8f9d408ca76c9eca20c871ee1a9c8757",
  "status": 404
}
```

**Recommended post-timeout recovery:** generate the `client_order_id` yourself before submitting (never let the
CLI auto-generate it — you cannot recover an ID you never knew). On timeout, poll
`order get-by-client-id --client-order-id <yours>` and branch on:

- exit 0 → the order exists; parse `status` / `id` from stdout.
- exit 1 **and** stderr JSON has `"status": 404` and `"code": 40410000` → the order was never accepted; safe to retry.
- exit 1 with any other `status` → transport/API problem; retry the *lookup*, not the submit.
- exit 2 → credentials; do not retry.

Discriminate on the parsed `status` field of the stderr JSON, not on the human message text.
There is **no** `--nested` flag on `get-by-client-id`, so the mleg children may not be rolled up here — **UNVERIFIED**.

### 2.4 `alpaca order cancel`

```
Usage: alpaca order cancel [flags]
  --order-id   order id
```

Missing-flag error (stderr, exit 1):

```json
{
  "code": 0,
  "error": "--order-id required (see 'alpaca order cancel --help' for examples)",
  "hint": "",
  "status": 0
}
```

Success output shape **UNVERIFIED** — `alpaca order cancel --schema` reports
`no response schema available for "alpaca order cancel"` (exit 1). The Alpaca API returns `204 No Content` for a
successful cancel, so expect empty stdout and exit 0; confirm before relying on it.

### 2.5 `alpaca order cancel-all` — MUTATING, no confirmation, no dry-run

`Usage: alpaca order cancel-all` — takes no flags. `--dry-run` is rejected (`unknown flag: --dry-run`).
**Not executed during this verification.** Response schema (from `--schema`, no API call):

```
// Delete all orders — returns an array of:
{
  id: string;      // orderId
  status: number;  // http response code
}
```

Per Alpaca: "If an order is no longer cancelable, the server will respond with status 500 and reject the request."

### 2.6 `alpaca order replace`

```
Usage: alpaca order replace [flags]
  --advanced-instructions, --client-order-id, --limit-price, --notional,
  --order-id, --qty, --stop-price, --time-in-force, --trail
```

Note `--trail` here (single flag), versus `--trail-price` / `--trail-percent` on `submit`. There is **no**
`--dry-run` on `replace` (verified: `unknown flag: --dry-run`), and **no `--legs`** — you cannot re-price an mleg
spread's legs through `replace`.

---

## 3. Account, clock, positions

### 3.1 `alpaca account get`

`Usage: alpaca account get` — no flags. Verified live output (paper account):

```json
{
  "account_number": "PA3ELCLN8HZS",
  "accrued_fees": "0",
  "balance_asof": "2026-08-27",
  "buying_power": "400000",
  "cash": "100000",
  "created_at": "2026-08-29T01:15:54.706878Z",
  "crypto_status": "ACTIVE",
  "currency": "USD",
  "equity": "100000",
  "id": "5f094444-555e-4952-a931-407eb7a73f61",
  "initial_margin": "0",
  "intraday_adjustments": "0",
  "last_equity": "100000",
  "last_maintenance_margin": "0",
  "long_market_value": "0",
  "maintenance_margin": "0",
  "multiplier": "4",
  "non_marginable_buying_power": "100000",
  "options_approved_level": 3,
  "options_buying_power": "100000",
  "options_trading_level": 3,
  "pending_reg_taf_fees": "0",
  "portfolio_value": "100000",
  "regt_buying_power": "200000",
  "short_market_value": "0",
  "shorting_enabled": true,
  "sma": "0",
  "status": "ACTIVE"
}
```

All money fields are **strings**. `options_approved_level` / `options_trading_level` are **numbers**.
`options_trading_level: 3` = spreads permitted, which is what an mleg credit spread needs.
Sibling commands: `alpaca account config get`, `alpaca account config set`, `alpaca account activity list`,
`alpaca account activity list-by-type`, `alpaca account portfolio`.

### 3.2 Market clock — the command is `alpaca clock markets`, NOT `alpaca clock get`

There is **no `alpaca clock get`**. The only subcommand is:

```
Usage: alpaca clock markets [flags]
  --markets    comma-separated list of markets
  --time       instead of the current time, use this time for the clock
```

The response is **an envelope with a `clocks` array covering ~13 venues by default** — there is no top-level
`is_open`. Verified, filtered to NYSE:

```json
{
  "clocks": [
    {
      "is_market_day": true,
      "market": {
        "acronym": "NYSE",
        "mic": "XNYS",
        "name": "New York Stock Exchange",
        "timezone": "America/New_York"
      },
      "next_market_close": "2026-08-31T16:00:00-04:00",
      "next_market_open": "2026-08-31T09:30:00-04:00",
      "phase": "closed",
      "phase_until": "2026-08-31T04:00:00-04:00",
      "timestamp": "2026-08-28T21:43:37.359749656-04:00"
    }
  ]
}
```

Fields: `is_market_day` (bool), `market.{acronym,mic|bic,name,timezone}`, `next_market_close`,
`next_market_open`, `phase` (observed: `"closed"`), `phase_until`, `timestamp`. Note `is_market_day: true` on a
day the market is closed — it means "this venue trades on this calendar day", not "open now". Use `phase`.

**For a simple open/closed boolean, use the raw API instead** — it returns the classic v2 clock:

```
$ alpaca api GET /v2/clock
{
  "is_open": false,
  "next_close": "2026-08-31T16:00:00-04:00",
  "next_open": "2026-08-31T09:30:00-04:00",
  "timestamp": "2026-08-28T21:43:37.478805444-04:00"
}
```

Options venue is `OPRA` (MIC `OPRA`) in `clock markets` — its `next_market_close` is `16:15`, not `16:00`.

Market calendar is `alpaca calendar market --market XNYS --start ... --end ... --timezone ...` (**not**
`alpaca calendar`).

### 3.3 `alpaca position list`

`Usage: alpaca position list` — no flags of its own. Returns a **bare JSON array**; verified `[]` on the empty
account, exit 0.

Element schema (`alpaca position list --schema`):

```
asset_class, asset_id, asset_marginable (bool), avg_entry_price, avg_entry_swap_rate,
change_today, cost_basis, current_price,
exchange: "AMEX"|"ARCA"|"BATS"|"CRYPTO"|"NASDAQ"|"NYSE"|"NYSEARCA"|"OTC",
lastday_price, market_value, prev_swap_rate, qty, qty_available,
side: "long"|"short", swap_rate, symbol,
unrealized_intraday_pl, unrealized_intraday_plpc, unrealized_pl, unrealized_plpc,
usd (object; LCT/non-USD accounts only)
```

All numerics are strings except `asset_marginable`.

Single position: `alpaca position get --symbol-or-asset-id AAPL`. Verified miss (stderr, exit 1):

```json
{
  "code": 40410000,
  "error": "position does not exist",
  "hint": "",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/positions/AAPL",
  "request_id": "01e13e08e301b5eaebc39209c826cf91",
  "status": 404
}
```

---

## 4. `alpaca doctor`

`Usage: alpaca doctor` — no flags. **Output is human-readable text, not JSON, and ignores `ALPACA_OUTPUT=json`.**
Exact verified output on the healthy paper setup (exit 0):

```
Alpaca CLI 0.0.14
  Go:       go1.24.0
  OS/Arch:  linux/amd64

Config:     /home/ianwalmsley/.config/alpaca
  ✓ config directory does not exist (ok when using env vars)
  ✓ no saved profiles configured (using env var credentials)
  ✓ active profile: paper
  ✓ API key credentials from env (ALPACA_API_KEY + ALPACA_SECRET_KEY)

Connectivity:
  Trading:  https://paper-api.alpaca.markets
  ✓ trading API: connected
  Data:     https://data.alpaca.markets
  ✓ data API: connected

Update:
  ✓ up to date (0.0.14)

All checks passed.
```

What it checks: CLI/Go/platform version; config directory presence; saved profiles; which profile is "active";
credential source (env vars vs profile); the resolved **trading base URL** and a live authenticated call to it;
the **data base URL** and a live call to it; and a network check for a newer CLI release.

On failure it prints `✗` lines, then emits an error object on stderr and **exits 1**:

```json
{
  "code": 0,
  "error": "some checks failed",
  "hint": "",
  "status": 0
}
```

Verified failing run (deliberately bogus credentials, `ALPACA_LIVE_TRADE=true`):

```
Connectivity:
  Trading:  https://api.alpaca.markets
  ✗ trading API: unauthorized. (HTTP 401)
  Data:     https://data.alpaca.markets
  ✗ data API: <html>
<head><title>401 Authorization Required</title></head>
...
 (HTTP 401)
```

Note the data-API failure path dumps **raw nginx HTML** into the report. Parse `doctor` output by exit code only.

**`doctor` makes network calls, including an update check.** Do not put it on a hot path.

---

## 5. Exit codes (verified)

| Code | Meaning | Verified case |
|---|---|---|
| `0` | success (including `--dry-run` and `--schema`) | `account get`, `order list`, all dry-runs |
| `1` | **everything else**: API errors, missing flags, unknown flags/commands, bad `--legs` JSON, bad `--jq`, missing credentials, `doctor` failures | 404, 422, `--order-id required`, `unknown flag`, `authentication required` |
| `2` | **HTTP 401 only** | bogus API key/secret against `/v2/account` |

The docs' "1 = API error, 2 = auth failure" is misleading: **absent credentials exit 1, not 2.** Only a rejected
401 from the server yields 2. Branch on the parsed stderr JSON, using exit code only as a coarse gate.

### 5.1 Two distinct stderr JSON shapes

Both go to **stderr**; stdout is empty on error. Both are pretty-printed multi-line JSON with sorted keys.

**(a) Client-side / local error** — no HTTP call was made. `code` and `status` are always `0`, and
`method`/`path`/`request_id` are **absent**:

```json
{
  "code": 0,
  "error": "--order-id required (see 'alpaca order get --help' for examples)",
  "hint": "",
  "status": 0
}
```

**(b) API error** — carries HTTP context:

```json
{
  "code": 40410000,
  "error": "order not found for 00000000-0000-0000-0000-000000000000",
  "hint": "",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/orders/00000000-0000-0000-0000-000000000000",
  "request_id": "fc793dbec9a709b5257ff69a4bfadff0",
  "status": 404
}
```

`status` is the HTTP status. `code` is Alpaca's numeric error code (observed: `40410000` = not found,
`40010001` = validation). `request_id` matches the response `X-Request-Id` header — log it for support.
`hint` is a CLI-authored human string, often empty; it is **not** stable, do not match on it. Observed hints:

- `"Validation error. Check parameter values — common issues: invalid qty, bad price, unknown symbol, or missing required fields."` (422)
- `"Invalid credentials. Run \`alpaca profile login\` to re-authenticate."` (401)

401 is the one case where `code` stays `0` but `status` is `401`:

```json
{
  "code": 0,
  "error": "unauthorized.",
  "hint": "Invalid credentials. Run `alpaca profile login` to re-authenticate.",
  "method": "GET",
  "path": "https://paper-api.alpaca.markets/v2/account",
  "request_id": "dff08a5747b54ffcd3264ddfb9d2350c",
  "status": 401
}
```

Note also the `error` string can contain a **literal newline**:
`"authentication required\nHint: run \`alpaca profile login\` to authenticate"`. Don't assume single-line.

---

## 6. Output formats

**There is no `--output` flag.** The docs' `--output json` does not exist. The knobs are:

| Flag | Behaviour |
|---|---|
| *(default)* | pretty-printed JSON, 2-space indent, **keys sorted alphabetically**, trailing newline |
| `--csv` | CSV: one header row + data rows |
| `--jq <expr>` | filter the JSON through an embedded jq (gojq); output is JSON |
| `-q`, `--quiet` | suppress non-data output (warnings, hints, color) |
| `--schema` | print the response schema and exit — **no API call**, no credentials needed (verified exit 0 with all env unset) |

Environment equivalent: `ALPACA_OUTPUT=json|csv` (verified: `ALPACA_OUTPUT=csv` behaves exactly like `--csv`).
`.env` in this repo sets `ALPACA_OUTPUT=json`, i.e. the default.

### 6.1 `--jq`

Verified:

```
$ alpaca account get --jq '.buying_power'
"400000"

$ alpaca account get --jq '{eq: .equity, bp: .buying_power}'
{
  "bp": "400000",
  "eq": "100000"
}

$ alpaca account get --jq '.nonexistent'
null                      # exit 0 — a missing field is NOT an error
```

Bad expression → local error, exit 1:

```json
{
  "code": 0,
  "error": "--jq: unexpected token \"syntax\"",
  "hint": "",
  "status": 0
}
```

`--jq` applies only to a **successful** response. On an API error the full error JSON still goes to stderr
untouched, and stdout is empty (verified with `order get --order-id 000...0 --jq '.id'`).

Scalar output is JSON-encoded — `.buying_power` yields `"400000"` **with quotes**. Strip them or use `-r`-style
post-processing yourself; there is no `--raw` flag.

### 6.2 `--csv`

```
$ alpaca account get --csv
account_number,accrued_fees,balance_asof,buying_power,cash,created_at,...
PA3ELCLN8HZS,0,2026-08-27,400000,100000,2026-08-29T01:15:54.706878Z,...
```

An empty array still prints the **header row** (verified: `order list --csv` and `position list --csv` on an
empty account each printed only headers, exit 0). So "CSV output has 1 line" means zero rows, not an error.

**CSV cannot represent nested objects.** `alpaca clock markets --markets XNYS --csv` produced a Go map dump:

```
clocks
[map[is_market_day:true market:map[acronym:NYSE mic:XNYS name:New York Stock Exchange timezone:America/New_York] next_market_close:2026-08-31T16:00:00-04:00 ...]]
```

Never use `--csv` for `clock markets`, or for any order read with `--nested`.

### 6.3 `--csv` + `--jq` = silent empty output — DANGEROUS

```
$ alpaca account get --csv --jq '.equity'
                       # stdout: EMPTY
                       # stderr: EMPTY
exit 0
```

Nothing is printed and the exit code is `0`. If your wrapper ever sets both (e.g. `ALPACA_OUTPUT=csv` in the
environment plus a `--jq` in code), you get a silent empty success. **Explicitly pass `--csv` never, or unset
`ALPACA_OUTPUT`, when using `--jq`.**

### 6.4 `--quiet`

`--quiet` suppresses warnings, hints and colour. It does **not** suppress error JSON on stderr — verified: a 404
with `--quiet` printed the full error object and exited 1. It does not change stdout data either
(`account get --quiet --jq '.status'` → `"ACTIVE"`). Env equivalent: `ALPACA_QUIET=true` (undocumented, verified).

### 6.5 Diagnostics

`-v` / `--verbose` — one summary line per request on stderr:

```
GET https://paper-api.alpaca.markets/v2/account → 401 (106ms)
```

`--trace` — timing breakdown on stderr:

```
trace: GET https://paper-api.alpaca.markets/v2/account
  dns:     3ms
  tcp:     36ms  (35.194.67.18:443)
  tls:     48ms
  ttfb:    44ms
  total:   134ms → 200
```

`--debug` — full headers + bodies on stderr. **Credentials are redacted** (verified with canary values):

```
→ GET https://paper-api.alpaca.markets/v2/account
→ Apca-Api-Key-Id: [REDACTED]
→ Apca-Api-Secret-Key: [REDACTED]
→ User-Agent: APCA-CLI/0.0.14 linux/amd64
← X-Request-Id: 17adace18a97c1db5a34554b91228028
...
← {"message": "unauthorized."}
```

`--debug` is safe to enable in logs with respect to API keys. It will still print **request bodies**, so an order
submit's payload lands in your logs.

`--timeout <int>` — HTTP timeout in seconds, default 30. `--timeout 0` did **not** error and did **not** time out
(a normal request succeeded), so 0 appears to mean "no timeout" rather than "fail immediately" — treat 0 as
unsafe. Timeout error shape is **UNVERIFIED** (could not force one).

---

## 7. Paper vs live — how the CLI decides

### 7.1 Resolution (verified by watching the `Trading:` base URL in `doctor`)

Credentials come from, in effect: `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` env vars, else a saved profile under
`$ALPACA_CONFIG_DIR` (default `~/.config/alpaca`, files at 0600), selected by `--profile` / `-p` or
`ALPACA_PROFILE`. This machine currently has **no** `~/.config/alpaca` directory, so it is running purely on env
vars.

The endpoint is chosen by `ALPACA_LIVE_TRADE`:

| `ALPACA_LIVE_TRADE` | Resolved trading base URL |
|---|---|
| unset | `https://paper-api.alpaca.markets` |
| `false` | `https://paper-api.alpaca.markets` |
| `true` | **`https://api.alpaca.markets` (LIVE)** |
| `True` / `TRUE` / `TrUe` | **`https://api.alpaca.markets` (LIVE)** |
| `1` | `https://paper-api.alpaca.markets` |
| `t` | `https://paper-api.alpaca.markets` |
| `yes` | `https://paper-api.alpaca.markets` |
| `true1` | `https://paper-api.alpaca.markets` |

So the test is a **case-insensitive exact match on the string `true`** — not a general boolean parse. `1` does
*not* enable live trading.

`ALPACA_PAPER_TRADE` is **not read by the CLI at all** (it does not appear among the binary's `ALPACA_*` string
references; setting `ALPACA_PAPER_TRADE=false` left the base URL on paper). It belongs to the Alpaca **MCP
server**, not this binary. Verified precedence: `ALPACA_LIVE_TRADE=true` + `ALPACA_PAPER_TRADE=true` → **live**.

There is **no `--live` or `--paper` flag** on the root command or on `order submit` (`unknown flag: --live`).
`--live` / `--paper` exist only on `alpaca profile login`, where they choose which account a *saved profile*
targets. A saved profile created with `--live` would presumably route to live regardless of `ALPACA_LIVE_TRADE`;
that interaction is **UNVERIFIED** here (no profiles exist on this machine).

The data API base URL is `https://data.alpaca.markets` in both modes.

### 7.2 The `doctor` "paper" line is a trap

With `ALPACA_LIVE_TRADE=true`, `doctor` still printed:

```
  ✓ active profile: paper
  ...
  Trading:  https://api.alpaca.markets
```

`active profile: paper` refers to the *profile name*, not the environment. **The only reliable indicator is the
`Trading:` URL.** For a preflight assertion, either grep `doctor` output for `paper-api.alpaca.markets`, or
assert `os.environ.get("ALPACA_LIVE_TRADE","").lower() != "true"` and independently confirm the account number
starts with `PA` (paper accounts) / the key ID starts with `PK`. The current key is a `PK...` paper key and the
account is `PA3ELCLN8HZS`.

### 7.3 Commands that mutate live state

These hit non-GET endpoints. Only `order submit` has a `--dry-run`; the rest reject it with `unknown flag`
(verified for `replace`, `cancel-all`, `position close`, `api`). **None of them prompt for confirmation.**

| Command | Effect |
|---|---|
| `order submit` | places an order (**has `--dry-run`**) |
| `order replace` | amends a working order |
| `order cancel` | cancels one order |
| `order cancel-all` | cancels **every** open order, no confirmation |
| `position close` | liquidates a position (`--qty` / `--percentage`) |
| `position close-all` | liquidates **everything**; `--cancel-orders` also cancels open orders first |
| `option exercise` | exercises a contract (`--symbol-or-contract-id`) |
| `option do-not-exercise` | files a DNE (`--symbol-or-contract-id`) |
| `account config set` | changes account settings (`--no-shorting`, `--suspend-trade`, `--max-options-trading-level`, …) |
| `locate create` | creates a locate request (borrow fees) |
| `wallet transfer create` | **moves crypto off-platform** |
| `wallet whitelist add` / `delete` | changes withdrawal whitelist |
| `watchlist create/update/delete/add/remove` (+ `-by-name` variants) | benign, but writes |
| `api POST/PUT/PATCH/DELETE <path>` | **arbitrary mutation, bypasses every CLI guard including `--dry-run`** |
| `profile login` / `logout` / `switch` | writes/removes credentials on disk |
| `update` / `update --yes` | **replaces the binary in place**; `--yes` skips the prompt |

`alpaca api` is the sharpest edge: `alpaca api POST /v2/orders --body '{...}'` (or the same JSON on stdin) places
a real order with no dry-run and no validation. If you wrap this CLI, allowlist subcommands rather than passing
user-supplied argv through.

`alpaca update` reaching out to check for a new release also means `doctor` performs a network call to
GitHub-or-equivalent on every run.

---

## 8. Rate limits and retries

The binary **does** implement retries. Symbol names recovered from the (stripped) binary:

```
github.com/alpacahq/cli/internal/client.isRetryable
github.com/alpacahq/cli/internal/client.retryDelay
doWithRetry
retryAfter
retryCount
```

and the string `Retry-After` is present.

The published docs claim: *"Automatic retry on 429/5xx (max 3 attempts, respects `Retry-After`)"*, and
*"Alpaca's API has rate limits per account. High-frequency querying may trigger rate limiting."*

**The retry parameters are UNVERIFIED.** There is no base-URL override environment variable (the only `ALPACA_*`
vars the binary references are `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_CONFIG_DIR`, `ALPACA_LIVE_TRADE`,
`ALPACA_OUTPUT`, `ALPACA_PROFILE`, `ALPACA_QUIET`, `ALPACA_DEBUG`, `ALPACA_TRACE`, `ALPACA_VERBOSE`), so the CLI
cannot be pointed at a local 429-returning stub, and deliberately tripping Alpaca's real limiter was out of
scope. **UNKNOWN:** exact max attempt count, backoff curve, which status codes are retried, and whether
`order submit` (a non-idempotent POST) is retried at all.

**This is the single most important open question for order-path code.** If the CLI silently retries a POST that
actually succeeded but whose response was lost, you can double-submit. Mitigate by always supplying your own
`--client-order-id` — Alpaca rejects duplicates, so a retried POST with the same client ID cannot create a second
order. Do not let the CLI auto-generate the ID.

Also note `--timeout` defaults to **30s** per request. If retries wrap that, wall-clock time for one
`order submit` invocation may exceed 30s; set your own subprocess timeout well above it (e.g. 120s) and treat a
subprocess timeout as "unknown outcome" → reconcile via `order get-by-client-id`.

---

## 9. Environment variables (verified against the binary's own string table)

| Variable | Read by CLI? | Effect |
|---|---|---|
| `ALPACA_API_KEY` | yes | API key ID |
| `ALPACA_SECRET_KEY` | yes | secret key |
| `ALPACA_LIVE_TRADE` | yes | `true` (case-insensitive) → live base URL; anything else → paper |
| `ALPACA_PROFILE` | yes | active profile name (equivalent to `-p`) |
| `ALPACA_OUTPUT` | yes | `json` (default) or `csv` |
| `ALPACA_CONFIG_DIR` | yes | overrides `~/.config/alpaca` (verified: `doctor` reported `Config: /tmp/altcfg`) |
| `ALPACA_QUIET` | yes (undocumented) | as `--quiet` |
| `ALPACA_DEBUG` | yes (undocumented) | as `--debug` |
| `ALPACA_VERBOSE` | yes (undocumented) | as `--verbose` |
| `ALPACA_TRACE` | yes (undocumented) | as `--trace` |
| `ALPACA_PAPER_TRADE` | **no** | ignored — MCP server only |

Repo note: `.env` sets `ALPACA_PROFILE=` (empty). An empty `ALPACA_PROFILE` was not observed to break anything,
but the verification harness unset it defensively. Prefer leaving the variable **absent** over setting it empty.

---

## 10. Full command tree (from `alpaca --help-all`, v0.0.14)

```
account activity list | activity list-by-type | config get | config set | get | portfolio
api [METHOD] <path> [--body --query --use-data-api]
asset get | list
calendar market
clock markets
corporate-action get | list
data auction | auctions | bars | corporate-actions
     crypto bars|latest-bars|latest-quotes|latest-trades|quotes|snapshots|trades
     crypto-orderbook
     fixed-income latest-prices|latest-quotes
     forex latest|rates
     latest-bar | latest-bars | latest-quote | latest-quotes | latest-trade | latest-trades
     logo | meta conditions|exchanges
     multi-bars | multi-quotes | multi-snapshots | multi-trades | news
     option bars|chain|conditions|exchanges|latest-quotes|latest-trades|snapshot|trades
     quotes | screener most-actives|movers | snapshot | trades
doctor
locate create | get | list | quotes
option contracts | do-not-exercise | exercise | get
order cancel | cancel-all | get | get-by-client-id | list | replace | submit
position close | close-all | get | list
profile list | login | logout | switch
update
version
wallet list | transfer create|estimate|get|list | whitelist add|delete|list
watchlist add | add-by-name | create | delete | delete-by-name | get | get-by-name | list
          remove | remove-by-name | update | update-by-name
completion [bash|zsh|fish|powershell]
help
```

Useful for the order path:

```
alpaca option contracts --underlying-symbols SPY --type put \
  --expiration-date-gte 2026-09-15 --expiration-date-lte 2026-09-19 \
  --strike-price-gte 300 --strike-price-lte 320 --limit 100
```

Returns `{ "next_page_token": "...", "option_contracts": [ ... ] }` — **an envelope, and paginated**. Each
contract carries `symbol`, `strike_price`, `expiration_date`, `type`, `style`, `multiplier`, `size`, `status`,
`tradable`, `open_interest`, `open_interest_date`, `close_price`, `close_price_date`, `root_symbol`,
`underlying_symbol`, `underlying_asset_id`, `id`, `name`. Verified real output for `SPY260918P00315000`:

```json
{
  "close_price": "0.01",
  "close_price_date": "2026-08-11",
  "expiration_date": "2026-09-18",
  "id": "0eb8f377-135d-4499-bb78-94a3ac421172",
  "multiplier": "100",
  "name": "SPY Sep 18 2026 315 Put",
  "open_interest": "5753",
  "open_interest_date": "2026-08-26",
  "root_symbol": "SPY",
  "size": "100",
  "status": "active",
  "strike_price": "315",
  "style": "american",
  "symbol": "SPY260918P00315000",
  "tradable": true,
  "type": "put",
  "underlying_asset_id": "b28f4066-5c6d-479b-a2af-85dc1a8f16fb",
  "underlying_symbol": "SPY"
}
```

Note `--underlying-symbols` (plural) on `option contracts`, but `--underlying-symbol` (singular) on
`data option chain`.

---

## 11. Doc errata

Verified differences between <https://docs.alpaca.markets/us/docs/alpacas-cli> and the v0.0.14 binary:

| Docs say | Reality |
|---|---|
| `alpaca clock` | `alpaca clock markets` (and the shape is `{clocks:[...]}`, not `{is_open:...}`) |
| `alpaca calendar` | `alpaca calendar market` |
| `alpaca data crypto latest-quotes --symbol BTC/USD,ETH/USD` | flag is `--symbols` on the multi-symbol `data` commands; single-symbol ones (`data bars`, `data latest-bar`, …) use `--symbol`. Check `--help` per command. |
| `alpaca order get --order-id <id>` ✓ | correct |
| `alpaca option exercise --symbol-or-id <contract>` | flag is `--symbol-or-contract-id` (only `option get` uses `--symbol-or-id`) |
| `alpaca asset get --symbol AAPL` | flag is `--symbol-or-asset-id` |
| `alpaca position get --symbol AAPL` / `position close --symbol AAPL` | flag is `--symbol-or-asset-id` |
| `alpaca watchlist get --watchlist-id <id>` | present, plus `-by-name` variants |
| `--output json` / `ALPACA_OUTPUT` as a flag | **no `--output` flag exists**; use `--csv` or the `ALPACA_OUTPUT` env var |
| "2 = authentication failure" | 2 only for HTTP 401; missing credentials → 1 |
| `--dry-run` = "preview orders without execution" | true, but it validates **nothing** |
| multi-leg / `--legs` | undocumented on the site; shape confirmed here |

Also undocumented on the site: `--schema`, `--trace`, `--help-all`, `ALPACA_QUIET`/`ALPACA_DEBUG`/
`ALPACA_VERBOSE`/`ALPACA_TRACE`, and `alpaca order get-by-client-id`'s exact endpoint.

---

## 12. Open questions (UNKNOWN / UNVERIFIED)

These could not be settled without submitting a real order or tripping the rate limiter:

1. **Retry policy on `order submit`.** Attempt count, backoff, retried status codes, and whether a POST is
   retried. Highest-risk unknown for the order path. (§8)
2. **`limit_price` sign convention for a credit spread** — positive credit vs negative. (§1.6)
3. Whether the API rejects an mleg with `type: market`, with a stray `symbol`, with >4 legs, or with no `qty`.
   The CLI will happily send all of these. (§1.4)
4. Whether `"advanced_instructions": {}` is accepted by every order endpoint. (§1.3)
5. Success output/exit for `order cancel` (no schema published; expect 204 → empty stdout, exit 0). (§2.4)
6. Nested mleg read-back shape from `order get --nested` / `order list --nested`, and whether
   `order get-by-client-id` returns legs at all (it has no `--nested` flag). (§2.1, §2.3)
7. `--timeout` error shape, and whether `--timeout 0` truly means "no timeout". (§6.5)
8. Interaction between a saved profile created with `profile login --live` and `ALPACA_LIVE_TRADE=false`. (§7.1)
9. Whether `order list` filter values are validated server-side (`--status bogus` returned `[]`, not an error, on
   an empty account). (§2.1)

---

*Verification transcript notes: all commands run from a wrapper that sourced `/home/ianwalmsley/projects/alpaca/.env`
and unset `ALPACA_PROFILE`. No credential values appear in this document. No order was submitted; every
`order submit` invocation carried `--dry-run`. `order cancel-all`, `position close-all`, `account config set`,
`profile login` and `alpaca update` were deliberately not executed.*
