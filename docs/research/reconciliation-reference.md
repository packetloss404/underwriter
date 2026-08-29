# Reconciliation Reference — knowing the true state of orders and positions

Researched 2026-08-28. Sources are the `/us/` docs tree (raw markdown via the `.md` suffix), the
Trading API OpenAPI specs served under `docs.alpaca.markets/us/reference/*.md`, and the installed
`alpaca-py` client at `/home/ianwalmsley/projects/alpaca/.venv/lib/python3.12/site-packages/alpaca/trading/stream.py`.

Anything not directly evidenced in those sources is marked **UNKNOWN**.

**The one-line conclusion:** the `trade_updates` websocket is a *latency optimisation, not a source
of truth*. It delivers no assignment, exercise or expiry events, and it has no replay or resume
mechanism, so every gap in the connection is a permanent hole. Polling REST is mandatory, not
optional.

---

## 1. Trade updates stream — URL, auth, subscribe

Source: https://docs.alpaca.markets/us/docs/websocket-streaming.md (updated 2025-09-24)

| | |
|---|---|
| Paper | `wss://paper-api.alpaca.markets/stream` |
| Live | `wss://api.alpaca.markets/stream` |
| Protocol | RFC6455. JSON and MessagePack codecs both supported (`Content-Type: application/msgpack` to request msgpack) |

> Documented gotcha: the `trade_updates` stream from `wss://paper-api.alpaca.markets/stream` uses
> **binary frames**, unlike the text frames of the market data stream at
> `wss://data.alpaca.markets/stream`. A client that assumes text frames will silently receive nothing.

### Auth handshake (documented form)

```json
{ "action": "auth", "key": "{YOUR_API_KEY_ID}", "secret": "{YOUR_API_SECRET_KEY}" }
```

Authorized response:

```json
{ "stream": "authorization", "data": { "status": "authorized", "action": "authenticate" } }
```

Unauthorized response is identical with `"status": "unauthorized"`. If you send `listen` before
authenticating you get `{"stream":"authorization","data":{"status":"unauthorized","action":"listen"}}`.

### Auth handshake (the form alpaca-py actually sends)

`alpaca/trading/stream.py::_auth` sends a **different, legacy envelope** that the server still accepts:

```json
{ "action": "authenticate", "data": { "key_id": "...", "secret_key": "..." } }
```

It then asserts `msg["data"]["status"] == "authorized"`. Both shapes work; if you hand-roll a client,
use the documented `auth`/`key`/`secret` form.

### Subscribe

```json
{ "action": "listen", "data": { "streams": ["trade_updates"] } }
```

Ack:

```json
{ "stream": "listening", "data": { "streams": ["trade_updates"] } }
```

Semantics that matter: `streams` is the **full desired set**, not a delta. To add a stream you resend
every stream name. To stop, send an empty list. Streams that are unavailable are silently omitted
from the ack — **compare the ack list against what you asked for**; a successful ack does not mean
you are subscribed.

In-stream errors arrive as `{"action":"error","data":{"error_message":"..."}}` and the server then
**closes the connection**.

---

## 2. Event types and the multi-leg FILL payload

Every message has an `event` and an `order` field; `order` is the same object the REST API returns.

### Common events
| Event | Meaning | Extra fields |
|---|---|---|
| `new` | Order routed to exchanges for execution | — |
| `fill` | Order completely filled | `timestamp`, `price`, `qty`, `position_qty` |
| `partial_fill` | Fewer shares than remaining qty filled | `timestamp`, `price`, `qty`, `position_qty` |
| `canceled` | Your cancel request processed | `timestamp` |
| `expired` | Reached end of life per `time_in_force` | `timestamp` |
| `done_for_day` | Done executing for the day | — |
| `replaced` | Your replace request processed | `timestamp` |

`price` is the price *for this event*, which differs from `filled_avg_price` when there were partial
fills. `qty` is the quantity *for this event*, not `filled_qty`. `position_qty` is the resulting
position size (negative for short).

### Less common events
`accepted`, `rejected` (+`timestamp`), `pending_new`, `stopped`, `pending_cancel`, `pending_replace`,
`calculated`, `suspended`, `order_replace_rejected`, `order_cancel_rejected`.

### Terminal-state note

The REST `OrderStatus` enum (`us/reference/getallorders-1.md`) contains **`held`** and
`accepted_for_bidding`, which have **no corresponding documented stream event**. Full enum:

```
new, partially_filled, filled, done_for_day, canceled, expired, replaced,
pending_cancel, pending_replace, accepted, pending_new, accepted_for_bidding,
stopped, rejected, suspended, calculated, held
```

Treat `filled`, `canceled`, `expired`, `rejected` as terminal; everything else as live and requiring
a poll to resolve.

### Exact FILL payload for a multi-leg options order

Verbatim from the docs (`MultilegOptionsOrder` fill example). Note the structural differences from a
single-leg fill: `position_qtys` (plural, a map) replaces `position_qty`, and a top-level `legs`
array carries per-leg execution details.

```json
{
    "stream": "trade_updates",
    "data": {
        "at": "2025-01-21T07:32:40.70095Z",
        "event_id": "01JJ3WE73W5PG672TC4XACXH5R",
        "event": "fill",
        "timestamp": "2025-01-21T07:32:40.695569506Z",
        "order": {
            "id": "31cd620f-3bd5-41b7-8bb2-6834524679d0",
            "client_order_id": "fe999618-6435-497b-9fdd-a63d3da3615f",
            "created_at": "2025-01-21T07:32:40.678963102Z",
            "updated_at": "2025-01-21T07:32:40.699359002Z",
            "submitted_at": "2025-01-21T07:32:40.691562346Z",
            "filled_at": "2025-01-21T07:32:40.695569506Z",
            "expired_at": null,
            "cancel_requested_at": null,
            "canceled_at": null,
            "failed_at": null,
            "replaced_at": null,
            "replaced_by": null,
            "replaces": null,
            "asset_id": "00000000-0000-0000-0000-000000000000",
            "symbol": "",
            "asset_class": "",
            "notional": null,
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": "1.62",
            "order_class": "mleg",
            "order_type": "limit",
            "type": "limit",
            "side": "buy",
            "time_in_force": "day",
            "limit_price": "2",
            "stop_price": null,
            "status": "filled",
            "extended_hours": false,
            "legs": [
                {
                    "id": "3cbe69ef-241c-43ba-9d8c-09361930a1af",
                    "client_order_id": "e868fb88-ce92-442b-91be-4b16defbc883",
                    "created_at": "2025-01-21T07:32:40.678963102Z",
                    "updated_at": "2025-01-21T07:32:40.697474882Z",
                    "submitted_at": "2025-01-21T07:32:40.687356797Z",
                    "filled_at": "2025-01-21T07:32:40.695564076Z",
                    "expired_at": null,
                    "cancel_requested_at": null,
                    "canceled_at": null,
                    "failed_at": null,
                    "replaced_at": null,
                    "replaced_by": null,
                    "replaces": null,
                    "asset_id": "925af3ed-5c00-4ef1-b89b-e4bd05f04486",
                    "symbol": "AAPL250321P00200000",
                    "asset_class": "us_option",
                    "notional": null,
                    "qty": "1",
                    "filled_qty": "1",
                    "filled_avg_price": "1.6",
                    "order_class": "mleg",
                    "order_type": "",
                    "type": "",
                    "side": "buy",
                    "time_in_force": "day",
                    "limit_price": null,
                    "stop_price": null,
                    "status": "filled",
                    "extended_hours": false,
                    "legs": null,
                    "trail_percent": null,
                    "trail_price": null,
                    "hwm": null,
                    "ratio_qty": "1"
                },
                {
                    "id": "ec694de5-5028-4347-8f89-d8ea00c9341f",
                    "client_order_id": "0a1bf1e1-6992-4c23-85a6-9469bbe05f1a",
                    "asset_id": "9f8c3d65-f5f7-42cd-acbc-9636cc32d3b5",
                    "symbol": "AAPL250321C00380000",
                    "asset_class": "us_option",
                    "qty": "1",
                    "filled_qty": "1",
                    "filled_avg_price": "0.02",
                    "order_class": "mleg",
                    "side": "buy",
                    "time_in_force": "day",
                    "status": "filled",
                    "legs": null,
                    "ratio_qty": "1"
                }
            ],
            "trail_percent": null,
            "trail_price": null,
            "hwm": null
        },
        "price": "1.62",
        "qty": "1",
        "position_qtys": {
            "AAPL250321P00200000": "1",
            "AAPL250321C00380000": "1"
        },
        "legs": [
            {
                "execution_id": "69a70e98-f370-427d-bcd3-834dc4800aed",
                "qty": "1",
                "price": "1.6",
                "order_id": "3cbe69ef-241c-43ba-9d8c-09361930a1af",
                "symbol": "AAPL250321P00200000",
                "timestamp": "2025-01-21T07:32:40.695564076Z"
            },
            {
                "execution_id": "fb878d87-569e-49f3-b42e-a09ad06e3d3a",
                "qty": "1",
                "price": "0.02",
                "order_id": "ec694de5-5028-4347-8f89-d8ea00c9341f",
                "symbol": "AAPL250321C00380000",
                "timestamp": "2025-01-21T07:32:40.695569506Z"
            }
        ]
    }
}
```

Parsing traps in that payload:

- The **parent order has `symbol: ""`, `asset_class: ""`, and `asset_id` all-zeroes**. Never key
  state off the parent's symbol. Identity lives in `client_order_id` and in the leg symbols.
- The parent `qty`/`filled_qty` are in **spread units**, not contracts. Contract counts are
  `parent_qty * leg.ratio_qty`.
- Legs carry `order_type: ""` and `type: ""` — empty strings, not null. Strict enum parsers break here.
- Each leg has its **own auto-generated `client_order_id`**, distinct from the one you supplied on
  the parent. Only the parent's is yours.
- The single-leg example uses `"event_id"`/`"at"` — those appear on the mleg example but **not** on
  the older single-leg example in the same doc. Do not depend on `event_id` being present.

---

## 3. What the stream does NOT deliver — **assignment, exercise, expiry**

This is stated flatly in https://docs.alpaca.markets/us/docs/options-trading.md:

> **"Options assignments are not delivered through websocket events. To check for assignment activity
> (non-trade activity, or NTA events), you'll need to poll the REST API endpoints. Websocket support
> for NTAs is not currently available."**

So the stream carries **order lifecycle events only**. Everything below is invisible to it and must
be polled from `GET /v2/account/activities`:

| Event | Activity type | What it does to your position |
|---|---|---|
| Option assignment (you were short) | `OPASN` + `OPTRD` | Option position removed; underlying stock position created |
| Option exercise (yours or auto-ITM) | `OPEXC` + `OPTRD` | Option position removed; underlying stock position created |
| OTM expiry | `OPEXP` | Option position flattened, nothing else |
| ITM auto-exercise at expiry | behaves as exercise (`OPEXC` + `OPTRD`) | as above |
| ITM sell-out for insufficient buying power | an **automated order** — so it *does* hit `trade_updates` as a normal fill | Option position closed by an order you did not place |
| Option corporate action | `OPCA` (with sub-types) | Contract terms/symbol changed |
| Non-standard cash deliverable | `OPCSH` | Cash settlement |

Also not on the stream: dividends, fees, interest, journals, splits, mergers, name/symbol changes.
Any of these can move cash or change a symbol under you.

### The paper-trading trap

From the same page:

> **"On PAPER NTAs are synced at the start of the following day. While your balance and positions are
> updated instantly, NTAs on PAPER will be visible in the Activities endpoint only the next day."**

This is the single most dangerous fact for a paper-trading agent. On paper:

- the websocket will never tell you about an assignment or expiry, **and**
- the activities endpoint will not show it until the next day.

The **only** same-day signal on paper is that the position vanishes from `GET /v2/positions` (and
cash/buying power moves). Therefore, on paper, position-diffing against your own expected state is
the primary detector, and activities are a next-day audit reconciliation, not a live feed.

---

## 4. Reconnection semantics — the stream is lossy

**UNKNOWN in the affirmative sense; lossy in every practical sense.** The docs contain no statement
about delivery guarantees, replay, resume tokens, sequence numbers, or gap detection for
`trade_updates`. There is:

- no `since` / `since_id` / `Last-Event-Id` parameter on the trade updates websocket,
- no sequence number in the payload that would let you detect a gap (`event_id` is a ULID and is not
  documented as gap-detectable, and it is absent from the single-leg example),
- no documented buffering of events while a client is disconnected.

The client implementation confirms the design intent. In `alpaca/trading/stream.py`, `_run_forever`
catches `websockets.WebSocketException`, closes, backs off (`_reconnect_min_backoff` 1.0s to
`_reconnect_max_backoff` 30.0s), then calls `_start_ws()`, which does connect → auth → `listen`. It
**passes no cursor of any kind**. Nothing in the reconnect path could request missed events even if
the server supported it.

Contrast: Alpaca *does* offer replay elsewhere and documents it explicitly — the Broker API's
`Subscribe to Activity Events (SSE)` endpoint takes `since` / `since_id` and honours the
`Last-Event-Id` reconnect header, replaying historical events first. The trade updates websocket
offers no equivalent. That asymmetry is the strongest available evidence that the websocket has no
replay.

**Conclusion: polling is mandatory.** Treat every disconnect as a definite gap. A reconnect must be
followed by a REST reconciliation sweep covering the disconnect window plus a safety margin, not by
an assumption that the stream caught up.

Additional loss modes beyond disconnection:

- `alpaca-py` sets `max_queue: 1024` on the websocket. A slow handler can overflow the buffer.
- Its handler dispatch is `await`ed inline in `_consume`; a slow or blocking handler stalls reads.
- A `listen` ack that silently omits `trade_updates` (see §1) leaves you connected and receiving nothing.
- `_run_forever` swallows non-`WebSocketException` errors and continues, so a persistent parse error
  can look like a healthy but silent stream. Track "time since last message" as a liveness metric.

---

## 5. Account activities — `GET /v2/account/activities`

Source: https://docs.alpaca.markets/us/reference/getaccountactivities-2.md (OpenAPI, updated 2026-05-27)

Two paths:

```
GET /v2/account/activities
GET /v2/account/activities/{activity_type}
```

Base URL: `https://paper-api.alpaca.markets` (paper) / `https://api.alpaca.markets` (live).
Auth: `APCA-API-KEY-ID` + `APCA-API-SECRET-KEY` headers.

### Query parameters (verbatim from the OAS)

| Param | Type | Default | Notes |
|---|---|---|---|
| `activity_types` | array (comma-separated) | — | Filter by type. **Cannot be combined with `category`.** Not valid on the `/{activity_type}` path. |
| `category` | string | — | `trade_activity` \| `non_trade_activity`. Mutually exclusive with `activity_types`. |
| `order_id` | uuid | — | "Filter activities associated with a specific order. Useful for retrieving the fills that make up a completely filled order." |
| `date` | string | — | Filters by **`created_at`, not settlement date**. `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`. |
| `until` | string | — | Created before this date (exclusive). |
| `after` | string | — | Created after this date (exclusive). |
| `direction` | string | `desc` | `asc` \| `desc` |
| `page_size` | integer | `100` | Max 100 **unless `date` is specified**, in which case all results are returned and there is no cap. |
| `page_token` | string | — | The `id` of the last activity on the current page. |

Explicit warning in the OAS on `date`: *"For non-trade activities such as fees, the creation date is
typically the day after the trade date (in UTC)."* So `created_at` and the economic event date differ
for NTAs — this is exactly the paper-NTA lag described in §3, and it is real on live too.

### Options-relevant activity type codes — verified against the OAS enum

The authoritative list is the `ActivityType` enum in `getaccountactivities-2`:

```
FILL, TRANS, MISC, ACATC, ACATS, CFEE, CGD, CSD, CSW, DIV, DIVCGL, DIVCGS,
DIVFEE, DIVFT, DIVNRA, DIVROC, DIVTW, DIVTXEX, FEE, INT, INTNRA, INTTW, JNL,
JNLC, JNLS, MA, NC, OPASN, OPCA, OPCSH, OPEXC, OPEXP, OPTRD, PTC, PTR, REO,
REORG, SPIN, SPLIT, FOPT, OCT
```

The options ones, spelled exactly:

| Code | Meaning |
|---|---|
| `FILL` | Order fills, partial and full. Applies to options unchanged. |
| `OPASN` | Option assignment |
| `OPEXC` | Option **exercise** |
| `OPEXP` | Option expiration |
| `OPTRD` | Option trade — the underlying stock leg produced by an exercise or assignment |
| `OPCA` | Option corporate action (sub-types `DIV.CDIV`, `MA.CMA`, `NC.SNC`, `SPLIT.FSPLIT`, `SPIN`, …) |
| `OPCSH` | Option cash deliverable for non-standard contracts |

> **Documentation conflict — use `OPEXC`.** The prose page
> https://docs.alpaca.markets/us/docs/account-activities.md lists the exercise code as **`OPXRC`**.
> The OpenAPI enum, the Broker API reference, and the worked examples on
> https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events.md all say **`OPEXC`**.
> Three sources to one; `OPEXC` is correct. Defensive move: accept both strings when parsing, and
> never use an exhaustive-match parser that throws on an unrecognised activity type — treat unknown
> types as "something happened, re-sync positions".

Also note `FEE` sub-types relevant to options: `ORF` (Options Regulatory Fee), `OCC` (Options
Clearing Corporation Fee), `TAF`, `CAT`, `REG`.

### Worked payloads (verbatim from the NTA doc)

Exercise of 2 AAPL 150 calls:

```json
[
  { "id": "20190801011955195::5f596936-6f23-4cef-bdf1-3806aae57dbf",
    "activity_type": "OPEXC", "date": "2023-07-21", "net_amount": "0",
    "description": "Option Exercise", "symbol": "AAPL230721C00150000",
    "qty": "-2", "status": "executed" },
  { "id": "20190801011955195::5f596936-6f23-4cef-bdf1-3806aae57dbf",
    "activity_type": "OPTRD", "date": "2023-07-21", "net_amount": "-30000",
    "description": "Option Trade", "symbol": "AAPL",
    "qty": "200", "price": "90", "status": "executed" }
]
```

Assignment of 2 short AAPL 150 calls:

```json
[
  { "activity_type": "OPASN", "date": "2023-07-01", "net_amount": "0",
    "description": "Option Assignment", "symbol": "AAPL230721C00150000",
    "qty": "2", "status": "executed" },
  { "activity_type": "OPTRD", "date": "2023-07-01", "net_amount": "30000",
    "description": "Option Trade", "symbol": "AAPL",
    "qty": "-200", "price": "150", "status": "executed" }
]
```

OTM expiry:

```json
[
  { "activity_type": "OPEXP", "date": "2023-07-21", "net_amount": "0",
    "description": "Option Expiry", "symbol": "AAPL230721C00150000",
    "qty": "-2", "status": "executed" }
]
```

Reading these: `qty` sign is the **change to the option position** (`-2` removes 2 long contracts;
`+2` on `OPASN` removes 2 short contracts). The `OPTRD` `qty` is in **underlying shares** (200 = 2
contracts × 100) and its sign is the change to the stock position. The `price` on `OPTRD` is the
strike — in the exercise example the doc's prose says $150 strike while the JSON shows `"price": "90"`,
which is an error in Alpaca's own example. Trust the prose and the arithmetic (`net_amount` -30000 /
200 shares = $150), not the `price` field in that one sample.

`OPEXC` and `OPASN` also carry `status: "executed"`; the NTA `status` enum is
`executed | correct | canceled`. **A `canceled` or `correct` activity means a prior activity was
reversed or amended.** Do not treat activities as append-only-and-final.

### Incremental polling without gaps or duplicates

The `id` format is `{YYYYMMDDHHMMSSmmm}::{uuid}` — a timestamp-prefixed, lexicographically sortable
cursor. That is the thing to persist.

The safe loop:

1. Persist `last_seen_activity_id` (the full `::` string), not a timestamp.
2. Poll with `direction=asc` and `page_token=<last_seen_activity_id>`. With `asc`, *"results will
   begin with the activity immediately after the one specified"* — exclusive, so no duplicates.
3. Page forward with `page_size=100` until a short page comes back, advancing
   `last_seen_activity_id` to the last `id` of each page.
4. Commit the cursor **only after** the page has been durably applied to your state. Crash-before-commit
   replays the page; make the apply idempotent keyed on activity `id`.

Why `asc` and not `desc`: `desc` is the default and is the wrong choice for incremental sync — with
`desc` the token means "end *before* this ID", which walks backwards into history. Use `asc` for
tailing, `desc` only for "show me the most recent N".

Cold start (no cursor): use `after=<session_start>&direction=asc` and page forward, or `date=<today>`
which lifts the 100-row cap entirely and returns everything for the day in one response — the latter
is the cheaper way to bootstrap a day's state on restart.

Do not use `after`/`until` timestamps as the steady-state cursor. Activities sharing a
`created_at` second would be duplicated or skipped at the boundary, and NTAs are created up to a day
after the economic event, so a timestamp watermark can permanently step over a late-arriving NTA. The
`page_token` cursor is ID-ordered and does not have this problem — but see the caveat in §9.

---

## 6. Positions — shape, and how a spread is represented

Source: https://docs.alpaca.markets/us/reference/getallopenpositions.md (OpenAPI) and
https://docs.alpaca.markets/us/docs/working-with-positions.md

```
GET /v2/positions                       -> array of Position
GET /v2/positions/{symbol_or_asset_id}  -> single Position (404 if flat)
```

`Position` fields (required fields marked ✱):

| Field | Notes for options |
|---|---|
| `asset_id` ✱ | uuid — *"For options this represents the **option contract ID**"* |
| `symbol` ✱ | The **OCC symbol**, e.g. `AAPL250321P00200000` |
| `asset_class` ✱ | `us_option` for option contracts. Full enum includes `us_equity`, `us_option`, `crypto`, `crypto_perp`, `treasury`, `corporate`, `global_equity`, `us_index`, `us_equity_chain`, `ipo` |
| `exchange` ✱ | **Empty for options** |
| `qty` ✱ | Number of **contracts** (not shares). Signed by `side`. |
| `qty_available` ✱ | *"Total number of shares available minus open orders / locked for options covered call"* — i.e. contracts pledged as covered-call collateral are excluded |
| `side` ✱ | `long` \| `short` |
| `avg_entry_price` ✱ | Per contract, in premium terms |
| `cost_basis` ✱, `market_value` ✱, `current_price` ✱, `lastday_price` ✱, `change_today` ✱ | |
| `unrealized_pl` ✱, `unrealized_plpc` ✱, `unrealized_intraday_pl` ✱, `unrealized_intraday_plpc` ✱ | |
| `asset_marginable` ✱ | |
| `avg_entry_swap_rate`, `swap_rate`, `prev_swap_rate`, `usd` | LCT accounts only |

### A multi-leg spread is N positions, not one

**There is no spread object anywhere in the positions API.** A two-leg vertical fills as two
independent `Position` rows, one per OCC symbol. The docs confirm the model is unchanged for options:
*"the existing Positions API model will work with options contracts. There is not expected to be a
change to this model."*

The `trade_updates` fill payload confirms the same at execution time: `position_qtys` is a **map keyed
by leg symbol**:

```json
"position_qtys": { "AAPL250321P00200000": "1", "AAPL250321C00380000": "1" }
```

Each leg is identified by:
- `symbol` — the OCC contract symbol (the only stable natural key)
- `asset_id` — the option contract UUID
- and, at order time only, the leg's own auto-generated `client_order_id` and `ratio_qty`

**Consequence for the agent.** The grouping of legs into a strategy exists only in *your* state, not
Alpaca's. If your local record of "spread X = these two OCC symbols" is lost, nothing in the API will
reconstruct it — you would have to re-derive it from order history via
`GET /v2/orders?nested=true&asset_class=us_option`, matching on parent order IDs. Persist the
leg→strategy mapping durably, keyed on `client_order_id` and the leg OCC symbols, before submitting.

**Partial-leg risk.** Because legs are independent positions, a spread can end up half-closed —
assignment on the short leg leaves you long the other leg plus a stock position, and the positions
endpoint will show exactly that with nothing marking it as a broken spread. Detect this by comparing
the observed leg set against your recorded strategy, not by looking for a spread-level field.

`current_price` semantics, from the positions doc:
04:00–09:30 ET last premarket trade; 09:30–16:00 ET last trade; 16:00–22:00 ET last after-hours trade;
22:00–04:00 ET the official 16:00 closing price. On the Basic plan options quotes are *indicative and
modified*, so option `market_value` is an estimate, not a mark you should trade against.

---

## 7. Detecting a close you did not cause

There is no "position closed" event. `GET /v2/positions` returns only open positions, and
*"once a position is closed, it will no longer be queryable through this API."* Disappearance is the
signal, and disappearance alone does not tell you why.

Detection strategy, in order of reliability:

1. **Diff positions against your own expected state.** Snapshot `GET /v2/positions` on a schedule and
   after every reconnect. Any symbol you expected but do not see, or any qty that differs from your
   book, is an unexplained change. On paper this is the *only* same-day detector (§3).

2. **Attribute the cause from activities.** For each unexplained symbol, query
   `GET /v2/account/activities?activity_types=OPASN,OPEXC,OPEXP,OPCA,OPCSH,FILL,OPTRD&after=...`:

   | What you find | Cause |
   |---|---|
   | `OPEXP` on that symbol | Expired OTM, position flattened |
   | `OPASN` (+ matching `OPTRD`) | Assigned — you now hold/owe the underlying |
   | `OPEXC` (+ matching `OPTRD`) | Exercised, by you or by auto-ITM-exercise at expiry |
   | `FILL` with an `order_id` you never submitted | Alpaca liquidation / sell-out (§10) |
   | `FILL` with an `order_id` you *did* submit | Your own order — expected |
   | `OPCA` | Corporate action changed the contract; the symbol may have been renamed, not closed |
   | nothing at all | On paper: almost certainly an NTA not yet synced. Re-check next day. On live: unresolved — escalate. |

3. **Check for an unexpected new position.** Assignment and exercise *create* an equity position in
   the underlying. A new `us_equity` position you never ordered is a strong assignment signal and
   often arrives before the NTA does.

4. **`OPCA` is a rename, not a close.** A corporate action can change the contract symbol. Matching
   only on OCC symbol will read that as "position vanished" plus "unknown position appeared". Check
   `OPCA` activities before concluding a position was closed.

Practical rule: **never treat "not in `/v2/positions`" as "closed by me and reconciled".** Mark it
`closed_unattributed` and keep retrying attribution until an activity explains it or a human does.

---

## 8. From a `client_order_id` to definitive order state

```
GET /v2/orders:by_client_order_id?client_order_id={your_id}
```

Source: https://docs.alpaca.markets/us/reference/getorderbyclientorderid.md

- `client_order_id` is a **required query parameter**; the colon in the path is literal.
- Returns the full `Order` object; `200` on success. A `404` means no such order exists.
- Add `nested=true` on the list endpoints to roll legs up under `legs`; this single-order endpoint
  returns legs for `mleg` orders (`legs` is *"required if order class is `mleg`"*).

`client_order_id` is *"A unique identifier for the order. Automatically generated if not sent
(<= 128 characters)"*. Always supply your own — a UUID derived from your intent, generated **before**
the submission attempt and persisted before the HTTP call.

**The timeout-mid-submission recovery, which is the case that actually matters:**

1. Generate `client_order_id`, write `{intent, client_order_id, state: submitting}` to durable
   storage, `fsync`.
2. `POST /v2/orders` with that `client_order_id`.
3. On any ambiguous outcome — timeout, connection reset, 5xx, process death — **do not resubmit**.
   Call `GET /v2/orders:by_client_order_id?client_order_id=...`.
   - `200` → the order exists. Adopt the returned `status` as truth. Never resubmit.
   - `404` → it did not land. Retry with the **same** `client_order_id` is safe.
4. Retry with the same id, never a fresh one. A fresh id on retry is how you end up with two positions.

Alpaca's own guidance for the CLI (recorded in `docs/research/revalidation-alpaca-2026-08-28.md`) is
the same shape: *"If the order comes back it went through — do not resubmit. A 404 means retry is safe."*

**UNKNOWN:** what status code a duplicate `client_order_id` returns. The repo's earlier revalidation
records `409 Conflict`; the `postorder` OpenAPI spec documents only `200`, `403` (insufficient buying
power/shares) and `422` (unrecognised parameters), and does not mention duplicate-id handling at all.
Handle 409 and 422 both as "already exists, go look it up", and do not rely on the specific code.

Note also there is a race even with 404: an order accepted but not yet visible would return 404. The
window is small but nonzero. Mitigate by retrying the lookup a couple of times over a few seconds
before concluding the order did not land, and by preferring an idempotent lookup-first flow at
startup (§9) over blind resubmission.

---

## 9. Restart recovery sequence

The ordering below is chosen so that each step's blind spot is covered by a later step.

**Step 0 — load durable local state.** Every `client_order_id` you have ever generated with state
`submitting` or `live`, your leg→strategy map, and `last_seen_activity_id`. If you have no durable
state, you cannot fully recover — see the gaps below.

**Step 1 — resolve every in-flight submission.** For each `client_order_id` in `submitting`, call
`GET /v2/orders:by_client_order_id`. This must come first: it closes the window where you might
double-submit. Do this *before* opening the websocket, so you are not racing live events.

**Step 2 — open the websocket and start buffering.** Connect, auth, `listen`, verify the ack contains
`trade_updates`, and **queue events without applying them**. Opening now means events that occur
during steps 3–5 are captured rather than missed. Note the wall-clock time of a successful
subscription — everything before it is the gap you must fill by polling.

**Step 3 — pull open orders.**
```
GET /v2/orders?status=open&nested=true&limit=500&direction=asc
```
`nested=true` rolls mleg legs under the parent. Page with `after_order_id` (do not mix
`after_order_id` with `after`/`until` — the OAS says they are mutually exclusive). Reconcile against
your book: orders Alpaca has that you don't know about, and orders you think are live that Alpaca
does not list, are both anomalies.

Also pull recently closed orders — `status=closed&after=<last known good time>` — to learn the
terminal state of anything that completed while you were down.

**Step 4 — pull positions.**
```
GET /v2/positions
```
This is the **authoritative statement of what you own right now**, and it is the one call that cannot
lie by omission about an open position. Rebuild the position book from it, not from replayed events.
Diff against your recorded strategies to find broken/half-closed spreads.

**Step 5 — replay activities from your cursor.**
```
GET /v2/account/activities?direction=asc&page_token=<last_seen_activity_id>&page_size=100
```
Page to exhaustion. This is what explains *how* the positions in step 4 came to differ from your
book — fills you missed, assignments, expiries. On a cold start with no cursor, use
`date=<today>` (no page-size cap) or `after=<session start>&direction=asc`.

**Step 6 — attribute every discrepancy**, per §7. Anything still unexplained gets marked
`closed_unattributed` / `position_unexplained` and blocks new trading on that underlying.

**Step 7 — drain the buffered websocket queue**, discarding or idempotently re-applying events already
covered by steps 3–5. Only now go live.

**Step 8 — keep polling.** Positions and open orders on a slow loop (e.g. 30–60s), activities
incrementally, and a full re-sync after every reconnect. Rate limits (§11) constrain the floor.

### What it can still miss

- **Paper NTAs.** An assignment or expiry that happened today will not appear in activities until
  tomorrow (§3). Step 4 will show the position gone; step 5 will not explain it. This is a *known
  permanent gap on paper* and the agent must tolerate an unexplained-but-real position change rather
  than treating it as an error to retry through.
- **Orders submitted with a lost `client_order_id`.** If the process died between generating the id
  and persisting it, step 1 cannot look it up. Partial mitigation: in step 3, scan open+recent orders
  for any `client_order_id` matching your generator's namespace/prefix that you do not recognise.
  Prefix your ids deterministically (e.g. `agentname-{strategy}-{uuid}`) so this scan is possible.
- **`created_at` vs event date.** Activities are cursored by creation, and NTAs are created up to a
  day after the trade date. An NTA can therefore appear *after* a cursor position you have already
  passed in creation order — the ID cursor stays correct (it is creation-ordered), but any logic that
  assumes "activities arrive in economic-event order" will be wrong.
- **Reversals.** An activity with `status: canceled` or `correct` amends an earlier one. If your apply
  logic is insert-only you will double-count.
- **Corporate actions renaming contracts** (`OPCA`) — handled only if you check for them explicitly.
- **The gap itself is unbounded.** Because there is no replay (§4), the length of the outage
  determines how far back step 5 must reach. Persist the cursor durably or the recovery window is
  guesswork.
- **UNKNOWN:** whether activities are guaranteed to be visible immediately after the corresponding
  `trade_updates` fill on **live**. The docs make this guarantee for neither environment. Assume
  read-after-write lag exists and re-poll.

---

## 10. Alpaca liquidation before expiry — when and how it is signalled

From https://docs.alpaca.markets/us/docs/options-trading.md, verbatim:

> - In the event no instruction is provided on an ITM contract, the Alpaca system will exercise the
>   contract as long as it is **ITM by at least $0.01 USD**.
> - Alpaca Operations has tooling and processes in place to identify accounts which pose a buying
>   power risk with ITM contracts.
> - **"In the event the account does not have sufficient buying power to exercise an ITM position,
>   Alpaca will sell-out the position within 1 hour before expiry."**

And from the NTA page:

> "In cases where there is insufficient buying power or underlying positions to facilitate the
> exercise, the system will **generate an automated order for the liquidation of the position**."

### How it is signalled

Because it is *an automated order*, not a non-trade activity, it is the **one involuntary close that
does reach the websocket** — as an ordinary `new` → `fill` sequence on an order whose `id` and
`client_order_id` you did not create, and as a `FILL` activity with an unfamiliar `order_id`.

So the detector is: **a fill on an order ID that is not in your book**. Not a special event type.

Practical consequences:
- The trigger is *insufficient buying power to exercise*, evaluated against the account, not against
  the individual position. A cash shortfall elsewhere can liquidate a position that is fine on its own.
- The window is the final hour before expiry. An agent holding ITM options into expiration day must
  either close them itself before that window, or hold enough buying power to survive exercise
  (strike × 100 × contracts for a long call).
- **Do not compete with it.** If your agent also tries to close in that window you can race Alpaca's
  liquidation order and end up short/long the underlying. Freeze new orders on expiring ITM contracts
  before the final hour and let attribution catch up.
- Alpaca's own worked description of expiry-day behaviour also notes that from **15:30 ET on
  expiration day** it stops accepting orders to open new positions and begins evaluating holdings.
  **UNVERIFIED** — that 15:30 detail came from a secondary rendering of the options docs and is not in
  the `.md` source quoted above; the $0.01-ITM threshold and the 1-hour sell-out window **are** in the
  source. Treat 15:30 as a working assumption to confirm empirically on paper.

Related endpoints: `POST /v2/positions/{symbol_or_contract_id}/exercise` (no body; exercises **all**
held contracts of that symbol; rejected between market close and midnight) and the do-not-exercise
endpoint (`optiondonotexercise`) — the docs say DNE must be arranged via support, while the OAS
documents an endpoint, so **UNKNOWN** whether DNE is programmatically usable on a Trading API account.

---

## 11. Rate limits

### Market data API — documented, per plan

Source: https://docs.alpaca.markets/us/docs/about-market-data-api.md

| | Basic (free, default for paper and live) | Algo Trader Plus ($99/mo) |
|---|---|---|
| Equities historical API calls | **200 / min** | 10,000 / min |
| Equities websocket subscriptions | 30 symbols | Unlimited |
| Equities real-time coverage | IEX only | All US exchanges |
| Options historical API calls | **200 / min** | 10,000 / min |
| Options websocket subscriptions | **200 quotes** | 1,000 quotes |
| Options real-time coverage | Indicative Pricing Feed | OPRA |
| Historical data limitation | latest 15 minutes withheld | no restriction |

### Trading API — **UNKNOWN**

No page in the current `/us/` docs tree states a numeric rate limit for the Trading API
(`api.alpaca.markets` / `paper-api.alpaca.markets`). Checked and found nothing:
`trading-api.md`, `getting-started-with-trading-api.md`, `orders-at-alpaca.md`, `paper-trading.md`,
`api-keys.md`, `faq.md`, `trading-api-faq.md`, and the `llms.txt` index (whose only "Rate Limits"
entry is the Broker API page). The widely-cited figure of 200 requests/min per API key is **not
currently documented** — treat it as an unverified working assumption and measure empirically.

### Behaviour on breach

The only Alpaca page documenting breach behaviour is the **Broker API** rate limits page
(https://docs.alpaca.markets/us/docs/broker-api-rate-limits.md). It specifies:

- `HTTP 429 Too Many Requests` on breach.
- Headers on **every** response, not just 429s:
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (unix seconds).
- Guidance: *"do not retry before the time indicated by `X-RateLimit-Reset`"*, exponential backoff
  with jitter (1s → 2s → 4s → 8s, capped).

**`Retry-After` is not mentioned anywhere in Alpaca's documentation.** However the official Alpaca
CLI *"retries 429 and 5xx with exponential backoff up to 3 attempts, respecting `Retry-After`"*
(recorded in `docs/research/revalidation-alpaca-2026-08-28.md`), which implies the header is at least
sometimes present. **UNKNOWN** whether the Trading API returns `Retry-After`, and **UNKNOWN** whether
the Trading API returns the `X-RateLimit-*` headers at all — the documentation for those is scoped to
the Broker API.

**Design accordingly:** read `X-RateLimit-Remaining` if present and slow down before it hits zero;
prefer `Retry-After` when present; otherwise fall back to exponential backoff with jitter honouring
`X-RateLimit-Reset`; and never let a reconciliation sweep issue an unbounded burst — budget the
restart sweep (§9) so it fits inside one minute's allowance.

The websocket has its own constraint: `alpaca-py`'s `stream.py` carries a comment that abandoned
half-open sockets *"keep consuming the single-connection slot and cause HTTP 429s"* — implying **one
concurrent trade-updates connection** per account, and that a leaked connection manifests as a 429 on
the next connect. Close sockets deterministically on shutdown.

---

## Design summary for the agent

1. `trade_updates` is a latency optimisation. **REST is the source of truth.** Never let stream state
   be the only record of a position.
2. **Poll unconditionally**, not just on reconnect: positions + open orders on a slow loop, activities
   incrementally by ID cursor.
3. **Generate and durably persist `client_order_id` before submitting.** Recover ambiguous submissions
   with `GET /v2/orders:by_client_order_id`. Retry with the same id or not at all.
4. **Persist the leg→strategy map yourself** — Alpaca has no concept of a spread in the positions API.
5. **Treat any unexplained position change as unresolved, not as an error to retry through**, and
   block trading on that underlying until it is attributed.
6. **On paper, expect a one-day NTA lag** and design the attribution logic to tolerate it.
7. **Do not hold ITM options into the final hour before expiry** without buying power to exercise, and
   do not race Alpaca's liquidation order.

## Sources

- https://docs.alpaca.markets/us/docs/websocket-streaming.md
- https://docs.alpaca.markets/us/docs/options-trading.md
- https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events.md
- https://docs.alpaca.markets/us/docs/account-activities.md
- https://docs.alpaca.markets/us/reference/getaccountactivities-2.md
- https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md
- https://docs.alpaca.markets/us/docs/working-with-positions.md
- https://docs.alpaca.markets/us/reference/getallopenpositions.md
- https://docs.alpaca.markets/us/reference/getallorders-1.md
- https://docs.alpaca.markets/us/reference/getorderbyclientorderid.md
- https://docs.alpaca.markets/us/reference/postorder.md
- https://docs.alpaca.markets/us/docs/about-market-data-api.md
- https://docs.alpaca.markets/us/docs/broker-api-rate-limits.md
- `/home/ianwalmsley/projects/alpaca/.venv/lib/python3.12/site-packages/alpaca/trading/stream.py`
- `/home/ianwalmsley/projects/alpaca/docs/research/revalidation-alpaca-2026-08-28.md`
