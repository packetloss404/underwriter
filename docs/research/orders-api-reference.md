# Alpaca Orders API — Reference for an Options Credit-Spread Agent

Researched 2026-08-28 against the live Alpaca docs. Every quoted string below is
verbatim from an Alpaca source; the source file is named at each point. Anything
not explicitly documented is marked **UNKNOWN**.

Primary sources (fetch the `.md` suffix for raw markdown + embedded OpenAPI JSON):

| Short name | URL |
| --- | --- |
| `postorder` | https://docs.alpaca.markets/us/reference/postorder.md |
| `level3` | https://docs.alpaca.markets/us/docs/options-level-3-trading.md |
| `options-trading` | https://docs.alpaca.markets/us/docs/options-trading.md |
| `orders-at-alpaca` | https://docs.alpaca.markets/us/docs/orders-at-alpaca.md |
| `getallorders` | https://docs.alpaca.markets/us/reference/getallorders-1.md |
| `patchorder` | https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md |
| `deleteorder` | https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md |
| `deleteallorders` | https://docs.alpaca.markets/us/reference/deleteallorders-1.md |
| `trade-events-sse` | https://docs.alpaca.markets/us/reference/subscribetotradev2sse.md |

> Note on the docs: the human-facing Level 3 page and the OpenAPI spec embedded in
> the `postorder` reference disagree in emphasis. Where they conflict, the OpenAPI
> spec is the authoritative contract — it is the schema the API actually validates
> against. This matters enormously for question 2.

---

## 1. Complete POST /v2/orders payload for a put credit spread

### Documented schema facts

From the OpenAPI `CreateOrderRequest` schema in `postorder`:

- Required at top level: `["type", "time_in_force"]`.
- `symbol` — "Asset symbol, required for all order classes except for `mleg`"
- `side` — "Required for all order classes except for mleg."
- `qty` — "number of shares to trade. Can be fractionable for only market and day order types. **Required for `mleg` order class, represents the number of units to trade of this strategy.**"
- `legs` — "list of order legs (<= 4)", `maxItems: 4`
- `MLegOrderLeg` required fields: `["symbol", "ratio_qty"]` (so `side` and `position_intent` are schema-optional, but see below — position intent is required in practice for options and every doc example sets both)
- `ratio_qty` — "proportional quantity of this leg in relation to the overall multi-leg order qty"
- `order_class` enum: `simple`, `bracket`, `oco`, `oto`, `mleg`, `""`. For options: "simple (or \"\")" and "mleg (required for multi-leg complex option strategies)"
- `OrderType` — "**Multileg Options trading: market, limit.**" (no `stop` / `stop_limit` for mleg; `options-trading` confirms: "`stop` and `stop_limit` are only available for single-leg orders")
- All scalar values are typed `"type": "string"` in the schema — `qty`, `ratio_qty`, `limit_price` are all strings.

Confirmations requested by the task, all **CONFIRMED**:

| Claim | Status |
| --- | --- |
| `order_class` must be `"mleg"` | CONFIRMED — "mleg (required for multi-leg complex option strategies)" |
| No top-level `symbol` | CONFIRMED — "required for all order classes except for `mleg`" |
| No top-level `side` | CONFIRMED — "Required for all order classes except for mleg." |
| No top-level `position_intent` | CONFIRMED by every doc example — intent is per-leg. The field exists at top level for *single-leg* options orders. |
| All values as strings | CONFIRMED — schema types are `string` throughout; every doc example quotes them |
| `ratio_qty` GCD-reduced | CONFIRMED — see §1.2 |
| `position_intent` per leg | CONFIRMED — `MLegOrderLeg.position_intent` |

### 1.1 Verbatim doc example (a *debit* put spread, from `level3`)

This is Alpaca's own "Long Put Spread" example — buy the 210 put, sell the 190 put:

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "1.25",
  "time_in_force": "day",
  "legs": [
    {
      "symbol": "AAPL250117P00210000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_open"
    },
    {
      "symbol": "AAPL250117P00190000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_open"
    }
  ]
}
```

**Alpaca's docs contain no put *credit* spread example.** Every Level 3 example is a
debit structure or a roll. The payload in §1.2 is therefore *constructed* by applying
the documented schema — it is not quoted from Alpaca.

### 1.2 Put credit spread payload (constructed from the documented schema)

Sell the nearer-the-money 195 put, buy the further-OTM 190 put, same expiry.
Net credit received: $1.20 per spread. Width $5.00, so max loss $500 − $120 = $380.

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "-1.20",
  "time_in_force": "day",
  "legs": [
    {
      "symbol": "AAPL250117P00195000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_open"
    },
    {
      "symbol": "AAPL250117P00190000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_open"
    }
  ]
}
```

Notes:

- `qty` is the number of *strategy units* (spreads), not contracts. With `qty: "3"`
  and `ratio_qty: "1"` on each leg, each leg trades 3 contracts.
- **GCD rule** (`level3`, verbatim): "each leg's `leg_ratio` must be in its simplest
  form. In other words, the greatest common divisor (GCD) among the `leg_ratio`
  values for the legs must be 1." Example given: ratios 4 and 2 "will reject this
  order… the user should enter it as 1:2 instead". For a 1:1 vertical this is
  automatically satisfied.
  (The docs call the field `leg_ratio` in prose and `ratio_qty` in the schema. The
  wire field is **`ratio_qty`**.)
- **Coverage rule** (`level3`, verbatim): "Starting on day zero of Options Level 3
  trading, an MLeg order is accepted only if all its legs are covered within the same
  MLeg order. For example, an MLeg order containing two short call legs would be
  rejected". A put credit spread's short 195 put is covered by the long 190 put, so
  a vertical credit spread satisfies this.
- **No equity legs**: "MLeg orders that include an equity leg are not supported at
  this time."
- **Sub-penny rule** (`orders-at-alpaca`): "Limit price >=$1.00: Max Decimals= 2 /
  Limit price <$1.00: Max Decimals = 4". Whether this test uses the absolute value
  of a negative mleg limit price is **UNKNOWN**. Recommendation: always round net
  credit to 2 decimals.

---

## 2. limit_price sign convention for a CREDIT spread — **NEGATIVE**

**This is the single most important finding, and the human-facing docs actively
mislead on it.**

Verbatim from the `CreateOrderRequest.limit_price` description in the OpenAPI spec
embedded in `postorder`:

> "Required if type is `limit` or `stop_limit`.
> In case of `mleg`, the limit_price parameter is expressed with the following notation:
> - A positive value indicates a debit, representing a cost or payment to be made.
> - A negative value signifies a credit, reflecting an amount to be received."

The identical sentence appears in the `PatchOrderRequest.limit_price` description in
`patchorder`, so the convention holds for replacement too.

**Answer: for a credit spread, `limit_price` is NEGATIVE — the signed net price, not
the absolute credit.** A spread you expect to collect $1.20 for is submitted as
`"limit_price": "-1.20"`. Submitting `"1.20"` would instruct Alpaca to *pay* $1.20 to
enter the position — an order that would either fill catastrophically or (more likely)
sit unfilled while the agent believes it is short a spread.

### Corroborating evidence from a real fill

The `MultilegOptionsOrderResponse` example in `postorder` is a 3:1 call spread. Parent
and legs:

| | `qty` | `ratio_qty` | `side` | `filled_qty` | `filled_avg_price` |
| --- | --- | --- | --- | --- | --- |
| parent | `"1"` | — | `""` | `"1"` | `"1.28"` |
| leg 1 | `"3"` | `"3"` | `buy` | `"3"` | `"0.43"` |
| leg 2 | `"1"` | `"1"` | `sell` | `"1"` | `"0.01"` |

3 × 0.43 − 1 × 0.01 = **1.28** = the parent `filled_avg_price`. So the parent price is
the *signed net* of the legs (buys positive, sells negative), per strategy unit. A
structure whose sells outweigh its buys therefore produces a negative net — exactly
the credit case.

### Why the Level 3 page looks like it contradicts this

`level3` shows only positive limit prices, and its examples are consistent with the
rule rather than contradicting it:

- "Long Call Spread" (`1.00`), "Long Put Spread" (`1.25`) — both **debit** structures. Positive is correct.
- "Iron Condor" (`1.80`) — its legs as written are buy 190P, sell 195P, sell 205C, buy 210C. That is a *long* (debit) iron condor as Alpaca has written it. Positive is correct.
- "Roll a Call Spread" (`2.05`) — a roll whose net can go either way; positive here means a net debit to roll.

None of them is a credit structure, which is why no negative appears. The Level 3 page
simply never exercises the credit case. It is not evidence against the spec.

### Corroborating: cost-basis arithmetic in `level3`

The cost-basis worked example for a call credit spread says, verbatim:

> "**Net Price** = (15−10)=(15 - 10) =$5 credit … However, for cost-basis purposes, a
> credit (positive $5) effectively reduces the overall cost, so it becomes **-$5** in
> the order's net debit/credit calculation."

Alpaca internally represents a credit as a negative net price. Same convention.

### Recommendation for the agent

Treat this as a hard invariant with an assertion, because a sign error is silent and
expensive:

- Opening a credit spread → `limit_price` **< 0**, magnitude = target net credit.
- Closing that spread → `limit_price` **> 0**, magnitude = net debit paid to close (§9).
- Assert the sign before every submission; refuse to submit a credit-spread open whose
  computed `limit_price` is ≥ 0.

Because this is a paper-tradeable claim, **verify it once against the paper API before
going live** — submit a far-OTM put credit spread with a deliberately unfillable
negative limit price and confirm it is accepted (status `new`/`accepted`) rather than
422-rejected. That single test costs nothing and removes the only real residual doubt.

---

## 3. time_in_force for multi-leg options — **`day` and `gtc` are both valid**

Resolved. The premise that "the OpenAPI spec says day only" does **not** hold against
the current spec. Findings:

1. There is exactly **one** `TimeInForce` schema in the `postorder` spec, with enum
   `["day","gtc","opg","cls","ioc","fok"]`, and its description says verbatim:
   > "The Time-In-Force values supported by Alpaca vary based on the order's security type… - Equity trading: day, gtc, opg, cls, ioc, fok. **- Options trading: day, gtc.** - Crypto trading: gtc, ioc."
2. There is **no separate mleg request schema**. `CreateOrderRequest` is the only order
   creation schema, and it references that same `TimeInForce`. Nothing in the spec
   narrows TIF for `order_class: mleg`.
3. `options-trading` prose agrees: "`time_in_force` must be `day` or `gtc`".
4. `orders-at-alpaca` has an "Options Orders" TIF support matrix. Its rows are GTC/DAY/
   IOC/FOK/OPG/CLS and its **columns are order types** (Market, Limit, Stop, Stop
   Limit) — not single-leg vs multi-leg. GTC = Yes and DAY = Yes across all four
   columns; IOC, FOK, OPG, CLS = No everywhere.

**Conclusion: `day` and `gtc` are both documented as valid for options, and no
mleg-specific restriction exists anywhere in the current docs or spec.** Every mleg
example in the docs happens to use `"day"`, which is likely the origin of the
"day only" belief.

Caveat worth acting on: `orders-at-alpaca` documents an **aged order policy** —
"Alpaca… will automatically cancel GTC orders 90 days after creation… This will take
place on the `expires_at` date at 4:15 pm ET. The orders will remain in pending_cancel
until canceled by the execution venue". For a credit-spread agent, `day` is the safer
default anyway: a stale GTC spread order repriced by the market is a liability.

---

## 4. `client_order_id`

Documented:

- **Max length: 128 characters.** Schema: `"maxLength": 128`, description verbatim:
  "A unique identifier for the order. Automatically generated if not sent. (<= 128 characters)"
- **Auto-generated if omitted.** Doc examples show a UUID (e.g. `"646b1fe6-b212-4f54-94c6-429e7bcdee04"`).
- **Retrievable by it**: `GET /v2/orders:by_client_order_id` — "Retrieves a single
  order specified by the client order ID."
- On the response object the field is described as "Client unique order ID".
- **On mleg, each leg gets its own distinct `client_order_id`**, auto-generated and
  different from the parent's. In the doc example the parent is
  `646b1fe6-b212-4f54-94c6-429e7bcdee04` while legs are
  `cc8cc104-fe43-476c-b25c-f62650fb73f9` and `0bd2d36d-4af2-4dfb-8418-333a5d5026fa`.
  **UNKNOWN** whether a caller-supplied parent `client_order_id` propagates to or
  constrains leg IDs.

**UNKNOWN** (not documented anywhere found):

- **Character set / allowed characters.** No pattern or charset constraint in the schema.
- **Uniqueness scope.** "unique" is asserted but never scoped — not stated whether it is
  per-account, per-account-per-day, or global. Given the retrieval endpoint is
  account-scoped, per-account is the reasonable assumption, but it is not documented.
- **Retention period.** Nothing states how long an ID stays reserved. The 90-day aged
  order policy governs GTC order lifetime, not ID retention.
- **Duplicate resubmission behavior.** **Not documented.** There is no statement of
  whether a repeated `client_order_id` returns the original order (idempotent) or an
  error, and **no error message text exists in the docs to quote.**

  Important contrast: Alpaca documents true idempotency explicitly where it exists, via
  a separate `Idempotency-Key` header on other endpoints (journals, transfers, tokenized
  mints) — e.g. "Multiple requests with the same key and identical request body will
  create only one transfer. A subsequent request returns the previously created transfer…
  If the same key is used with a different request body, the API returns `422
  Unprocessable Entity`." **`POST /v2/orders` does not document an `Idempotency-Key`
  header.** So `client_order_id` should *not* be assumed to give idempotent retry
  semantics. The documented `POST /v2/orders` responses are only 200, 403, 422.

- Practical guidance for the agent: a duplicate is most likely a 422, but since this is
  undocumented, **verify against paper** before relying on either behavior. Do not build
  crash-recovery on the assumption that resubmitting a duplicate ID safely returns the
  original order.

Related, and directly relevant to retry logic — `working-with-orders` FAQ, verbatim:

> "**What should I do if I receive a timeout message from Alpaca when submitting an order?**
> The order may have been sent to the market for execution. You should not attempt to
> resend the order or mark the timed-out order as canceled until confirmed by Alpaca
> Support or the trading team."

So on timeout the documented guidance is: **do not blind-retry.** Reconcile by querying
`GET /v2/orders:by_client_order_id` with the ID you sent.

---

## 5. Order status values and terminality

Full `OrderStatus` enum from the OpenAPI spec — **17 values**:

```
new, partially_filled, filled, done_for_day, canceled, expired, replaced,
pending_cancel, pending_replace, accepted, pending_new, accepted_for_bidding,
stopped, rejected, suspended, calculated, held
```

Common statuses (verbatim descriptions):

| status | description |
| --- | --- |
| `new` | "The order has been received by Alpaca, and routed to exchanges for execution. This is the usual initial state of an order." |
| `partially_filled` | "The order has been partially filled." |
| `filled` | "The order has been filled, and no further updates will occur for the order." |
| `done_for_day` | "The order is done executing for the day, and will not receive further updates until the next trading day." |
| `canceled` | "The order has been canceled, and no further updates will occur for the order. This can be either due to a cancel request by the user, or the order has been canceled by the exchanges due to its time-in-force." |
| `expired` | "The order has expired, and no further updates will occur for the order." |
| `replaced` | "The order was replaced by another order, or was updated due to a market event such as corporate action." |
| `pending_cancel` | "The order is waiting to be canceled." |
| `pending_replace` | "The order is waiting to be replaced by another order. The order will reject cancel request while in this state." |

Rare statuses — "these states only occur on very rare occasions":

| status | description |
| --- | --- |
| `accepted` | "The order has been received by Alpaca, but hasn't yet been routed to the execution venue. This could be seen often out side of trading session hours." |
| `pending_new` | "The order has been received by Alpaca, and routed to the exchanges, but has not yet been accepted for execution." |
| `accepted_for_bidding` | "The order has been received by exchanges, and is evaluated for pricing." |
| `stopped` | "The order has been stopped, and a trade is guaranteed for the order, usually at a stated price or better, but has not yet occurred." |
| `rejected` | "The order has been rejected, and no further updates will occur for the order. This state occurs on rare occasions and may occur based on various conditions decided by the exchanges." |
| `suspended` | "The order has been suspended, and is not eligible for trading." |
| `calculated` | "The order has been completed for the day (either filled or done for day), but remaining settlement calculations are still pending." |
| `held` | **Present in the enum but has NO description in either the status table or the OpenAPI description.** The only gloss anywhere is in `trade-events-sse`: "`held` For multi-leg orders, the secondary orders (stop loss, take profit) will enter this state while waiting to be triggered." That refers to bracket/OTO legs, not `order_class: mleg`. Its meaning for an mleg options order is **UNKNOWN**. |

### Terminal statuses

Four statuses are explicitly "no further updates will occur for the order":
**`filled`, `canceled`, `expired`, `rejected`**.

Separately, and this is the operationally load-bearing sentence (verbatim, appears in
both `orders-at-alpaca` and the OpenAPI description):

> "An order may be canceled through the API up until the point it reaches a state of
> either `filled`, `canceled`, or `expired`."

Note the asymmetry the agent must handle: **`rejected` is terminal for updates but is
not in the cancelability list.** Treat the terminal set as
`{filled, canceled, expired, rejected}` for state-machine purposes.

`replaced` is also effectively terminal for *that* order id — the order was superseded
and `replaced_by` points at its successor.

### Which statuses can an mleg order pass through?

**Not documented as an mleg-specific list — UNKNOWN.** No doc restricts the status set
by order class. Observed from the doc's own mleg example: parent and both legs reach
`filled`. The `postorder` examples also show `pending_new` and `accepted` on other
classes. Assume the full enum applies to the mleg parent, and note that **legs carry
their own independent `status` field** (both `"filled"` in the example).

### Trade-update stream events (`trade-events-sse`)

Worth wiring instead of polling. Common: `accepted`, `pending_new`, `new`, `fill`,
`partial_fill`, `canceled`, `expired`, `done_for_day`, `replaced`. Rarer: `rejected`,
`held`, `stopped`, `pending_cancel`, `pending_replace`, `calculated`, `suspended`,
`order_replace_rejected`, `order_cancel_rejected`, `trade_bust` ("Sent when a previously
reported execution has been canceled (\"busted\") by the upstream exchange"),
`trade_correct`, `restated`.

`trade_bust` and `trade_correct` deserve explicit handling — a fill the agent already
acted on can be revoked or repriced after the fact.

---

## 6. Partial fills on a multi-leg order

### What the docs actually say

The strongest statement is in `level3`, describing the *purpose* of mleg orders
(verbatim):

> "By bundling all legs together, the trade is executed as a single unit"

and, for the iron condor example:

> "Placing these four legs as a single MLeg order ensures they **fill together or not
> at all**. This reduces the risk of partial fills, which could otherwise leave the
> trader with unwanted market exposure or unbalanced positions."

And earlier: MLeg orders "reduce the chance of partial fills that could distort the
intended strategy."

### The answer

**Legs cannot fill independently in a way that breaks the strategy ratio.** The spread
executes as a unit — you will not end up short the 195 put with no long 190 put against
it. This is the whole point of the order class, and Alpaca states it plainly ("fill
together or not at all").

**But "all-or-nothing" applies to the *ratio*, not to the *quantity*.** Note the careful
hedging in Alpaca's own wording — "reduce the risk of", "reduce the chance of" — not
"eliminate". The order can fill in *strategy units*: with `qty: "5"`, 2 spreads may fill
and 3 remain open, leaving the parent `partially_filled`. Each leg fills in proportion,
so the position stays balanced at 2× the ratio. What cannot happen is one leg filling
while its pair does not.

For an agent submitting `qty: "1"`, this reduces to genuine all-or-nothing.
**For `qty` > 1, partial fill is a real state the agent must handle**, and the residual
open quantity is still a live working order that must be cancelled or managed.

### `filled_qty` on parent vs legs

From the `MultilegOptionsOrderResponse` example in `postorder` (a 3:1 call spread, fully
filled):

- **Parent**: `"qty": "1"`, `"filled_qty": "1"`, `"filled_avg_price": "1.28"`,
  `"side": ""`, `"symbol": ""`, `"order_class": "mleg"`, `"type": "limit"`,
  `"limit_price": "10"`, `"status": "filled"`.
- **Leg 1**: `"symbol": "AAPL241213C00250000"`, `"ratio_qty": "3"`, `"qty": "3"`,
  `"filled_qty": "3"`, `"filled_avg_price": "0.43"`, `"side": "buy"`,
  `"position_intent": "buy_to_open"`, `"status": "filled"`, `"legs": null`.
- **Leg 2**: `"symbol": "AAPL241213C00260000"`, `"ratio_qty": "1"`, `"qty": "1"`,
  `"filled_qty": "1"`, `"filled_avg_price": "0.01"`, `"side": "sell"`,
  `"position_intent": "sell_to_open"`, `"status": "filled"`, `"legs": null`.

Therefore:

| Field | On the parent | On a leg |
| --- | --- | --- |
| `qty` | number of **strategy units** (spreads) | `ratio_qty` × parent `qty` = **contracts** for that leg |
| `filled_qty` | strategy units filled | **contracts** filled on that leg |
| `filled_avg_price` | **signed net price per strategy unit** (3×0.43 − 1×0.01 = 1.28); positive = debit paid, negative = credit received | that leg's own average premium, always positive |
| `side` | `""` (empty) | `buy` / `sell` |
| `symbol` | `""` (empty) | the OCC contract symbol |
| `legs` | array of leg orders | `null` — "Always null for an order leg; legs are not nested beyond one level." |
| `type` | `"limit"` | `""` (empty) |
| `limit_price` | the signed net limit | `null` |

Practical consequences for the agent:

- **Never read `filled_qty` on the parent as a contract count.** Multiply by `ratio_qty`
  per leg.
- **The realized net credit is the parent `filled_avg_price`**, which under the §2
  convention will be **negative** for a filled credit spread. Multiply by 100 ×
  `filled_qty` for dollars.
- Legs must be fetched with `nested=true` on the list endpoint, or they arrive as
  separate flat orders (§ below).

### Retrieving legs

`getallorders` query param, verbatim: `nested` — "If true, the result will roll up
multi-leg orders under the legs field of primary order."

And on the `Order.legs` field: "When querying non-simple order_class orders in a nested
style, an array of Order entities associated with this order. Otherwise, null.
**Required if order class is `mleg`.**"

**Always pass `nested=true`** when polling for mleg orders. Also useful:
`asset_class=us_option` filters to options orders only.

---

## 7. Cancellation

`DELETE /v2/orders/{order_id}` — verbatim description:

> "Attempts to cancel an Open Order. If the order is no longer cancelable, the request
> will be rejected with status 422; otherwise accepted with return status 204."

Documented responses:

| Code | Meaning |
| --- | --- |
| `204` | "No Content" — cancel accepted |
| `422` | "The order status is not cancelable." |

Key points:

- **204 means the cancel request was *accepted*, not that the order is cancelled.** The
  order typically transitions to `pending_cancel` first. The agent must confirm the
  terminal `canceled` status via poll or the trade-updates stream — never assume.
- **Already filled → 422.** Cancelability rule (verbatim): "An order may be canceled
  through the API up until the point it reaches a state of either `filled`, `canceled`,
  or `expired`." A filled order returns 422.
- **`pending_replace` blocks cancellation**: "The order will reject cancel request while
  in this state."
- **Race condition to handle**: a cancel can return 204 and the order still fills, if the
  fill beats the cancel to the venue. Treat 204 as "requested", and 422 as "too late —
  go re-read the order, it may be filled."

**Cancelling a partially filled mleg**: **not explicitly documented — UNKNOWN.** The
general cancelability rule keys on status, and `partially_filled` is not in the
non-cancelable set `{filled, canceled, expired}`, so by the documented rule a
`partially_filled` order **is** cancelable, cancelling the unfilled remainder and
leaving the already-filled strategy units as an open position. Alpaca does not state
this for mleg specifically. **Verify on paper.** The agent must then reconcile: after
cancelling a partial, it holds a real spread position at a smaller size than intended.

`DELETE /v2/orders` (cancel all) — verbatim: "Attempts to cancel all open orders. A
response will be provided for each order that is attempted to be cancelled. If an order
is no longer cancelable, the server will respond with status 500 and reject the
request." Returns a per-order array of `{id, status}`. Blunt instrument; prefer
per-order cancels.

---

## 8. Order replacement (PATCH) for mleg

`PATCH /v2/orders/{order_id}` — "Replaces a single order with updated parameters. Each
parameter overrides the corresponding attribute of the existing order. The other
attributes remain the same as the existing order."

### Is mleg supported?

**Strong implicit evidence yes; never stated explicitly. Partially UNKNOWN.**

Evidence **for**: the `PatchOrderRequest.limit_price` description carries the mleg
notation verbatim —

> "Required if original order's `type` field was `limit` or `stop_limit`. **In case of
> `mleg`, the limit_price parameter is expressed with the following notation:** - A
> positive value indicates a debit… - A negative value signifies a credit…"

There would be no reason to document mleg sign semantics on the replace endpoint if
replace rejected mleg orders.

Evidence **against / gaps**: the endpoint's request examples are `Equity`, `IPO`, and
`Options` (single-leg: `{"limit_price": "11.25", "time_in_force": "day"}`) — **there is
no `MultilegOptions` PATCH example**, whereas `POST /v2/orders` *does* have one. The
PATCH response examples likewise omit the multileg case. No doc sentence says mleg is
replaceable, and none says it isn't. There is also no documented way to change
`ratio_qty` or leg composition via PATCH — the schema exposes `qty`, `time_in_force`,
`limit_price`, `stop_price`, `trail`, `client_order_id`.

### Documented replacement constraints (all apply)

- "Order cannot be replaced when the status is `accepted`, `pending_new`,
  `pending_cancel` or `pending_replace`."
- "A success return code from a replaced order does **NOT** guarantee the existing open
  order has been replaced. If the existing open order is filled before the replacing
  (new) order reaches the execution venue, the replacing (new) order is rejected, and
  these events are sent in the trade_updates stream channel."
- "While an order is being replaced, buying power is reduced by the larger of the two
  orders that have been placed."
- The 200 response is "The new Order object with **the new order ID**." — **replacement
  mints a new order id.** The old order goes to `replaced` with `replaced_by` set; the
  new one carries `replaces`.
- Notional orders cannot be replaced (irrelevant for options — `notional` is prohibited).

### Recommendation: use cancel-and-resubmit to reprice

Given that mleg PATCH support is documented only by implication, and given the explicit
warning that a successful PATCH does not guarantee replacement, **cancel-and-resubmit is
the safer reprice path for a credit-spread agent**:

1. `DELETE /v2/orders/{id}`
2. Poll/stream until the order reaches terminal `canceled` (not merely 204/`pending_cancel`)
3. Submit a fresh mleg order with the new negative `limit_price`

This costs a round trip but gives an unambiguous state machine, and avoids the
"replacement rejected because the original filled" ambiguity where the agent may
briefly believe it has no position while holding one. The cost is a window where
neither order is live — acceptable for a credit spread being repriced, not acceptable
if you are chasing a fast-moving exit.

If you do use PATCH: send `limit_price` **with the correct negative sign** (the same
convention as §2), handle `order_replace_rejected` from the stream, and follow
`replaced_by` to the new order id. **Verify mleg PATCH works on paper before depending
on it.**

---

## 9. Closing an open put credit spread

### Confirmations requested

| Claim | Status |
| --- | --- |
| `position_intent` flips to `buy_to_close` / `sell_to_close` | **CONFIRMED** by the roll examples in `level3` |
| `ratio_qty` stays the same | **CONFIRMED** — ratios describe the structure, and the roll examples reuse `"1"` for closing legs. GCD rule still applies. |
| `limit_price` sign flips | **CONFIRMED by the §2 rule** — closing a credit spread costs money, i.e. a debit, i.e. **positive** |

Also note `side` flips alongside intent: the leg you sold to open is now `side: "buy"` /
`buy_to_close`; the leg you bought to open is now `side: "sell"` / `sell_to_close`.

### Verbatim precedent from `level3` — the closing pair inside a roll

Alpaca's "Roll a Call Spread (strike price)" example, whose first two legs are exactly a
spread-closing pair:

```json
{
  "symbol": "AAPL250117C00200000",
  "ratio_qty": "1",
  "side": "buy",
  "position_intent": "buy_to_close"
},
{
  "symbol": "AAPL250117C00205000",
  "ratio_qty": "1",
  "side": "sell",
  "position_intent": "sell_to_close"
}
```

Note the shape: `buy` + `buy_to_close` on the previously-short leg, `sell` +
`sell_to_close` on the previously-long leg. That is the pattern to mirror.

### Closing payload (constructed — closes the §1.2 position)

Alpaca documents no standalone 2-leg closing mleg example; this applies the schema and
the roll precedent. Paying $0.40 to close a spread opened for $1.20 credit:

```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "0.40",
  "time_in_force": "day",
  "legs": [
    {
      "symbol": "AAPL250117P00195000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_close"
    },
    {
      "symbol": "AAPL250117P00190000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_close"
    }
  ]
}
```

`limit_price` is **positive `"0.40"`** — a debit paid. Net P&L per spread =
(1.20 − 0.40) × 100 = $80.

Caveats:

- **The coverage rule applied to a pure closing order is not explicitly documented.**
  `level3` says "an MLeg order is accepted only if all its legs are covered within the
  same MLeg order", framed around opening short legs. A both-legs-closing order reduces
  risk and contains no new short exposure, and it appears as a sub-structure of Alpaca's
  own roll examples, so it should be accepted — but a *standalone* 2-leg close is not
  exemplified. **Verify on paper.**
- If the agent ever wants to close for a *credit* (unusual for a credit spread, but
  possible on a roll), the sign flips negative again. Always derive the sign from the
  computed net, never hardcode it per direction.
- **Rolling is restricted**: "This restriction also impacts certain strategies, including
  rolling a short contract or rolling a calendar spread, since they would involve
  uncovered short legs within the same multi-leg order." A same-expiry vertical roll (as
  in the doc example) is fine; a calendar roll may be rejected.

---

## 10. Expiry, exercise, assignment, auto-liquidation

All verbatim from `options-trading` unless noted. **This section is where a credit-spread
agent gets hurt if it does nothing.**

### Automatic exercise of ITM contracts

> "By default, Alpaca will automatically exercise in-the-money (ITM) contracts at expiry."

> "In the event no instruction is provided on an ITM contract, the Alpaca system will
> exercise the contract as long as it is **ITM by at least $0.01 USD**."

So the ITM threshold is **$0.01**. Note this applies to *your long* leg. Your *short*
leg is subject to assignment at the counterparty's discretion — American style, so
**assignment can happen any time before expiry, not only at expiry.**

### Auto-liquidation — the one to design around

> "Alpaca Operations has tooling and processes in place to identify accounts which pose a
> buying power risk with ITM contracts."

> "In the event the account does not have sufficient buying power to exercise an ITM
> position, Alpaca will **sell-out the position within 1 hour before expiry**."

**Timing: within the 1 hour before expiry** — i.e. roughly after 3:00 pm ET on expiration
day for a 4:00 pm close. This is Alpaca acting on the account without instruction, at
whatever price it gets.

**Implication for the agent: never carry a short-ITM credit spread into the final hour of
expiration day.** Close or roll well before ~3:00 pm ET on expiry. Relying on the long
leg to cover the short leg through expiry hands price discovery to Alpaca's liquidation
desk and can produce a worse outcome than the spread's theoretical max loss (plus pin
risk and overnight assignment exposure if the legs are settled asynchronously).

Recommended hard rule: a scheduled flatten check that force-closes any open spread by
a configurable cutoff (e.g. 2:00 pm ET on expiration day), before Alpaca's window opens.

### Exercise API

> "Endpoint: `POST /v2/positions/{symbol_or_contract_id}/exercise` (no body)"

> "All available held shares of this option contract will be exercised."

> "Exercise requests will be processed immediately once received. Exercise requests
> submitted between market close and midnight will be rejected to avoid any confusion
> about when the exercise will settle."

Note: exercise is **all-or-nothing on the position** — no partial exercise.

### Do Not Exercise

> "To submit a Do-not-exercise (DNE) instruction, please contact our support team."

**There is a documented DNE API reference** (`https://docs.alpaca.markets/us/reference/optiondonotexercise.md`) even though this page routes you to support. Whether DNE is
programmatically available on a Trading API (non-Broker) account is **UNKNOWN** — the
prose says contact support.

### Assignment / expiry visibility — a real trap

> "Options assignments are not delivered through websocket events. To check for assignment
> activity (non-trade activity, or NTA events), you'll need to **poll the REST API
> endpoints**. Websocket support for NTAs is not currently available."

And, critically for development:

> "🚧 On PAPER NTAs are synced at the start of the following day. While your balance and
> positions are updated instantly, **NTAs on PAPER will be visible in the Activities
> endpoint only the next day**"

So: **the agent cannot learn about assignment or expiry from the trade-updates stream.**
It must poll `GET /v2/account/activities` for exercise/assignment/expiry NTAs, and on
paper those arrive a day late — meaning paper testing will *not* faithfully rehearse
assignment handling. Positions and balances do update immediately, so reconcile against
`GET /v2/positions` as the source of truth rather than activities.

NTA entry types for options "reflect exercise, assignment, and expiry" — details at
`https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events.md`.

---

## 11. Errors

### Documented HTTP responses for `POST /v2/orders`

Only three, verbatim from the OpenAPI spec:

| Code | Description |
| --- | --- |
| `200` | "Successful response" |
| `403` | "Forbidden\n\n**Buying power or shares is not sufficient.**" |
| `422` | "Unprocessable\n\n**Input parameters are not recognized.**" |

For `DELETE /v2/orders/{order_id}`: `204` "No Content", `422` "The order status is not
cancelable."

For `DELETE /v2/orders`: per-order `{id, status}` array; "If an order is no longer
cancelable, the server will respond with status 500 and reject the request."

So the coarse mapping the agent can rely on:

- **403 → insufficient buying power.** This is the credit-spread agent's most likely
  rejection (options buying power / maintenance margin for the spread width).
- **422 → malformed or invalid order.** This bucket absorbs bad `ratio_qty` GCD, invalid
  leg combinations, uncovered short legs, bad TIF, sub-penny violations, and presumably
  duplicate `client_order_id`.

### Error body shape

Alpaca returns `{"code": <int>, "message": "<string>"}`. The only concrete order-rejection
examples in the docs are the sub-penny ones (`orders-at-alpaca`), verbatim:

```json
{
  "code": 42210000,
  "message": "invalid limit_price 290.123. sub-penny increment does not fulfill minimum pricing criteria"
}
```

```json
{
  "code": 42210000,
  "message": "invalid stop_price 290.123. sub-penny increment does not fulfill minimum pricing criteria"
}
```

Note the code structure: `42210000` — the leading `422` mirrors the HTTP status, so codes
are `<http status><subcode>`. This lets the agent classify by prefix even for undocumented
codes.

One further exact message, for a fractional-equity case (not options, included only
because it is one of the few exact strings Alpaca publishes), verbatim:

> `"unable to open new notional orders while having open closing position orders"`

### Requested error messages — mostly UNDOCUMENTED

**There is no error-code catalogue in Alpaca's public docs.** I searched the docs index
for error/troubleshooting pages and found none. For each requested case:

| Case | Status |
| --- | --- |
| **Insufficient options level** | **UNKNOWN** — no message text published. The level rules are documented (`options-trading` trading-levels table: L1 covered call / cash-secured put, L2 + buy call/put, L3 + buy call spread / buy put spread) and the validation column says "User must have sufficient options buying power", but no rejection string. Likely 403. Note: **a put credit spread requires Level 3.** Check `GET /v2/account` (`options_approved_level`, `options_trading_level`) and `GET /v2/account/configurations` (`max_options_trading_level`) before trading rather than discovering it via rejection. |
| **Insufficient buying power** | **Partially documented** — HTTP `403` "Buying power or shares is not sufficient." No exact `message` string published. |
| **Invalid leg combination** | **UNKNOWN** — no message text. The *rules* are documented (max 4 legs, no equity legs, all legs covered, same underlying, GCD-reduced ratios). Expect 422. |
| **Uncovered short leg** | **UNKNOWN** — no message text. Rule verbatim: "an MLeg order is accepted only if all its legs are covered within the same MLeg order. For example, an MLeg order containing two short call legs would be rejected". Expect 422. |
| **Market closed** | **UNKNOWN** — no message text for options. For equities, `orders-at-alpaca`: "Orders not eligible for extended hours submitted after 4:00pm ET will be queued up for release the next trading day." Options **cannot** use extended hours ("`extended_hours` must be `false` or not populated"), so an options order outside RTH is presumably queued rather than rejected — but this is **not explicitly documented for options.** Check `GET /v2/clock` before submitting. |
| **Duplicate `client_order_id`** | **UNKNOWN** — see §4. No documented behavior and no message to quote. |
| **GCD violation** | **UNKNOWN** message; rule documented ("the system will reject this order"). |
| **Sub-penny** | **DOCUMENTED** — code `42210000`, message quoted above. |

### Recommendation

Because the error catalogue is undocumented, **do not pattern-match on message strings in
production logic.** Build the agent's error handling on:

1. HTTP status (403 = capital problem → back off, don't retry; 422 = our bug → do not
   retry, alert).
2. The numeric `code` prefix.
3. Log the full `message` verbatim for human diagnosis.

Then run a paper-account error-harvesting pass — deliberately submit each failure mode
(uncovered short leg, GCD 2:4, positive limit price on a credit spread, `ioc` TIF,
duplicate `client_order_id`, oversized order) and record the actual `code`/`message`
pairs. That will produce the catalogue Alpaca doesn't publish, and it is the only way to
get exact strings.

---

## Summary of items requiring paper-account verification

Ranked by how much damage a wrong assumption causes:

1. **Credit `limit_price` must be negative** (§2) — spec is explicit and corroborated by
   fill arithmetic, but verify once; a sign error is silent and expensive.
2. **Auto-liquidation window, 1 hour before expiry** (§10) — build the flatten-by-cutoff
   rule regardless; verify the exact behavior.
3. **Standalone 2-leg closing mleg order is accepted** (§9) — no doc example exists.
4. **Duplicate `client_order_id` behavior** (§4) — completely undocumented; do not assume
   idempotent retry.
5. **Cancelling a `partially_filled` mleg** (§7) — implied cancelable by the status rule,
   not stated for mleg.
6. **mleg PATCH support** (§8) — implied by the schema, no example. Prefer
   cancel-and-resubmit meanwhile.
7. **`gtc` on an mleg order** (§3) — documented as valid for options with no mleg carve-out,
   but every example uses `day`. Prefer `day` anyway.
8. **Exact error `code`/`message` pairs** (§11) — harvest them; none are published.
9. **Sub-penny rule against a negative limit price** (§1.2) — round credits to 2 decimals.
10. **Meaning of `held` on an mleg order** (§5) — undocumented for this order class.
