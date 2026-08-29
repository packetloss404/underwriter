# Gotchas

Failure modes verified during kickoff research that do **not** announce themselves.
Each one looks like a different problem than it is. Ordered by how much time they cost
if missed.

## 1. `expiration_date_lte` silently truncates the contract universe

`GET /v2/options/contracts` **does not error** when expiry bounds are omitted. It
returns only contracts expiring before the coming weekend. The chain simply comes back
thin.

This presents as a liquidity problem, a bad underlying, or a broken filter — never as a
missing query parameter. You will debug the wrong thing.

**Rule:** always send explicit `expiration_date_gte` *and* `expiration_date_lte`.
`underwriter.chain` refuses to build a request without both.

## 2. `options_trading_level` is not `options_approved_level`

```
options_trading_level = min(options_approved_level, max_options_trading_level)
```

A fresh paper account can report `options_approved_level: 3` while the effective level
is lower, because the account configuration caps it. Every multi-leg order then rejects
with a level error that reads like an *approval* problem, so you go looking at the
account approval rather than at the configuration.

**Rule:** gate on `options_trading_level`. `underwriter.preflight` does, and warns
explicitly when the two disagree. `max_options_trading_level` can be raised via
`PATCH /v2/account/configurations` — do this at setup, not mid-session.

## 3. Paper multi-leg fills are simulated against *modified* indicative quotes

We are on the Basic plan permanently, so option quotes are indicative derivatives, not
OPRA NBBO. Alpaca's paper engine checks marketability against those altered quotes, and
**the multi-leg fill model is entirely undocumented** — whether the documented ~10%
random partial-fill behaviour applies to a spread is unknown, and a partially filled
vertical contradicts all-or-nothing execution.

**Rule:** the official paper P&L is a number we report, not a number we trust. The
conservative shadow P&L modelling explicit bid/ask and slippage is the honest one, and
both belong in the submission side by side.

## 4. Open interest is not in the option chain snapshot

`OptionsSnapshot` carries `symbol`, `latest_trade`, `latest_quote`,
`implied_volatility` and `greeks` — and **no open interest**. It lives only on
`OptionContract`, from the separate `get_option_contracts` endpoint, so obtaining it
means a second call per underlying and a join on symbol.

A liquidity filter that treats missing open interest as a failure therefore rejects the
entire chain, for a reason that is not true.

**Rule:** `LiquidityPolicy.require_open_interest` defaults to `False`, so a missing value
is tolerated and recorded while an explicit low value still rejects. Calibration performs
the join deliberately, because measuring open interest is one of the things it exists to
do.

## 5. The Basic plan refuses recent SIP data outright

A bars query ending at `now` fails with:

```
403 Forbidden
{"message":"subscription does not permit querying recent SIP data"}
```

It does **not** silently degrade to a permitted feed — it errors, so a first live run
dies on the very first call.

Two fixes, and they are not equivalent. Passing `feed="iex"` succeeds but returns only
IEX's share of volume, and the prices differ (SPY closed 769.35 on SIP against 769.28 on
IEX). Backing the query's `end` off past the fifteen-minute embargo keeps us on
consolidated SIP, which is strictly better data.

**Rule:** `data.SIP_EMBARGO` is 20 minutes and `daily_closes` ends its window there. This
costs nothing, because realised volatility is computed from *completed* daily bars and
today's partial bar does not belong in it.

## 6. `alpaca order submit` defaults to `--type market`

For a multi-leg spread you must pass `--type limit` explicitly. Forgetting it submits a
market order on a spread — the exact thing the strategy spec forbids.

**Also:** omit `--symbol` entirely for `mleg`. The OAS says symbol is *"required for all
order classes except for `mleg`"*, where it lives on each leg instead.

## 7. Multi-leg credit orders take a NEGATIVE `limit_price`

For `mleg`, `limit_price` is the **signed net price**, not the absolute amount. Verbatim
from the OpenAPI `CreateOrderRequest.limit_price` description:

> A positive value indicates a debit, representing a cost or payment to be made.
> A negative value signifies a credit, reflecting an amount to be received.

A credit spread expected to collect $1.20 is submitted as `"limit_price": "-1.20"`.
**Closing it flips the sign positive**, because buying the spread back is a debit.

This does not error if you get it wrong. A positive limit price on a credit spread reads
as "I will pay $1.20 to enter this" — a gift to whoever takes the other side, plausibly
filled, and visible only as mysteriously bad P&L.

## 8. "All or nothing" applies to the ratio, not the quantity

Legs "fill together or not at all", so a naked leg is impossible. But with `qty: "5"`,
two spreads can fill while three keep working, leaving the parent `partially_filled` and
balanced but smaller. Treating a multi-leg order as binary filled/unfilled misstates open
risk.

The units also differ between parent and leg, in a way that silently corrupts P&L if
conflated:

| | `filled_qty` | `filled_avg_price` |
|---|---|---|
| Parent | strategy units (spreads) | signed net per unit — **negative** for a filled credit |
| Leg | contracts (`ratio_qty` x parent qty) | that leg's own premium, always **positive** |

Parent `side` and `symbol` come back as empty strings. **`nested=true` is mandatory when
listing orders**, or legs return as separate flat orders and reconciliation goes wrong
without complaint.

## 9. The broker does not de-duplicate `client_order_id`

Duplicate `client_order_id` behaviour on `POST /v2/orders` is **entirely undocumented**.
Alpaca documents real idempotency via an `Idempotency-Key` header on *other* endpoints
but not on order submission, so retry-safety cannot be assumed.

**Rule:** after a timeout or any unknown outcome, look the order up by client order ID
*before* considering a retry. If the lookup is inconclusive, refuse to submit. A missed
trade is an opportunity lost; a double-submitted spread is double the risk with no record
of why.

## 10. Alpaca may liquidate your position an hour before expiry

If buying power is insufficient for an ITM exercise, Alpaca "will sell-out the position
within 1 hour before expiry". That is a broker action we do not control, so the strategy
needs a hard flatten cutoff comfortably before ~15:00 ET on expiration day rather than
relying on our own exit rules to get there first.

## 11. Time in force: `day` and `gtc` are both valid

An earlier reading of an order-type matrix suggested `day` only. That was wrong — the
matrix columns are order types, not leg counts, and the spec has a single `TimeInForce`
schema with no separate multi-leg variant.

We still default to `day`, because nothing should rest overnight unmonitored, but it is a
parameter rather than a constraint.

## 12. On paper, assignment and expiry are invisible until the next day

Two separate gaps compound into one nasty blind spot.

The `trade_updates` websocket carries **order lifecycle events only**. Alpaca:
*"Options assignments are not delivered through websocket events... Websocket support for
NTAs is not currently available."* So assignment (`OPASN`), exercise (`OPEXC`), expiry
(`OPEXP`), option corporate actions and cash deliverables never appear on the stream at
all, and must be polled from `GET /v2/account/activities`.

But on paper, *"NTAs are synced at the start of the following day"*. So they are missing
from the activities feed too, until tomorrow.

**On the judged account, an assigned or expired position produces no event and no
activity record on the day it happens. The only same-day signal is the position quietly
disappearing from `GET /v2/positions`.**

**Rule:** position-snapshot diffing is the primary same-day truth source. A position that
vanishes with no fill we initiated is an assignment, expiry, or broker liquidation, and is
recorded as inferred rather than treated as a clean close. When the activity arrives the
next day it is attached to the inferred event rather than double-counted.

(Doc conflict worth knowing: the prose page says `OPXRC` for exercise; the OpenAPI enum
and worked examples say `OPEXC`. `OPEXC` is correct.)

## 13. The trade-updates stream is lossy and has no replay

No `since`, no `since_id`, no `Last-Event-Id`, no sequence number, and no gap-detectable
field in the payload. `alpaca-py`'s `stream.py` reconnects by backing off and calling
`_start_ws()` with no cursor of any kind. Alpaca's Broker API SSE endpoint *does*
document replay parameters, which makes the omission here look deliberate rather than
undocumented.

Three additional silent-loss modes: a `max_queue` of 1024 can overflow, a `listen`
acknowledgement can omit `trade_updates` while the socket stays happily connected, and
`_run_forever` swallows non-`WebSocketException` errors.

**Rule:** the stream is a latency optimisation, never the system of record. Every
disconnect is assumed to be a gap and is followed by a REST sweep, and time-since-last-
message is tracked as a liveness signal. Nothing streamed is authoritative without REST
confirmation.

## 14. A fill can arrive for an order you never placed

Alpaca's insufficient-buying-power sell-out before expiry is an *automated order*, so
unlike other non-trade activity it **does** reach the stream — as a fill against an order
ID we never created. Fill handling must tolerate an unknown order reference rather than
rejecting or crashing on it.

## 15. `--dry-run` validates nothing

It is a local JSON pretty-printer. It makes no HTTP call and exits 0 for every malformed
order tested: `--type` omitted (silently becomes `market`), `--symbol` passed alongside
`--legs` (emitted in the body rather than stripped), `--order-class` omitted, five legs
where help says four, `--qty` omitted, wrong-case `side`, unknown leg fields silently
dropped, and `--legs '[]'`, which omits the `legs` key entirely and prints a bare
`order_class: mleg` order with no legs.

**Exit 0 from `--dry-run` means "the CLI parsed my flags", never "Alpaca will accept
this".** It is useful for showing a human the payload and worthless as a correctness
check. We validate the payload ourselves before submission, because our validation is
the only validation that exists.

## 16. The CLI may retry a non-idempotent POST

The binary contains `client.isRetryable`, `client.retryDelay`, `doWithRetry`,
`retryCount` and a `Retry-After` string, and the docs claim three attempts on 429/5xx.
Whether `order submit` specifically is retried could not be confirmed — there is no
base-URL override, so the CLI cannot be pointed at a stub.

Combined with gotcha #9 (the broker does not de-duplicate `client_order_id`), that is an
unquantified double-submission risk on the most dangerous operation we perform.

**Rules:** always pass our own `--client-order-id`, never let it auto-generate. Reconcile
after any submission whose outcome is not a clean success. Look up via the separate
subcommand `alpaca order get-by-client-id --client-order-id <id>` — `order list` has no
client-order-id filter.

## 17. `alpaca doctor` is not a safety indicator

It prints `✓ active profile: paper` even when `ALPACA_LIVE_TRADE=true` and the resolved
base URL is `https://api.alpaca.markets`. That line names the *profile*, not the
environment. Only the `Trading:` URL is reliable.

`ALPACA_PAPER_TRADE` is not read by the CLI at all — it is MCP-server only, so it offers
zero protection here.

Note a deliberate divergence: the CLI's live toggle is a case-insensitive exact match on
the string `true`, so `ALPACA_LIVE_TRADE=1` leaves the CLI on paper, while our pydantic
config parses `1` as true and refuses to start. We are stricter, which is the safe
direction, but the two do not agree.

## 18. Three smaller CLI traps

- **`--csv` with `--jq` prints nothing at all** — empty stdout, empty stderr, exit 0.
  Silent data loss, and `.env` sets `ALPACA_OUTPUT`, so it can happen by accident. Pass
  `--output json` explicitly on every invocation rather than inheriting it.
- **Exit code 2 means HTTP 401 only.** Entirely absent credentials exit 1, so "2 = auth
  failure" is too coarse to branch on.
- **`alpaca clock get` does not exist.** It is `alpaca clock markets`, returning
  `{clocks:[...]}` across roughly thirteen venues with no top-level `is_open`. Use
  `alpaca api GET /v2/clock` for the simple boolean.

## 19. MCP multi-leg `legs` serialization is still broken

Issue #97 is untouched since 2026-07-01; fix PR #107 is open and unmerged; `overrides.py`
on `main` is still unpatched. **Do not put the MCP server on the order path.** The CLI's
`--legs` flag (v0.0.14) is verified working and satisfies the hackathon's MCP-or-CLI
requirement.

## 20. `alpaca-py` upstream only CI-tests Python 3.10 and 3.11

The 3.12/3.13/3.14 classifiers are auto-generated from a `^3.10.0` constraint, not a
tested matrix. We run 3.12. Also, alpaca-py floats on `pandas>=1.5.3`, so it will happily
resolve onto pandas 3.x that upstream has never exercised — **pin pandas explicitly**.
