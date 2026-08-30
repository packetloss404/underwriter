# Integration probes

What has actually been proven against the live Alpaca paper API, as opposed to
verified against documentation. The distinction matters: three things we
believed from the docs turned out wrong during this build, and every one was
caught by running the thing rather than reading about it.

## Probe 1 — multi-leg order submission and cancel

**2026-08-30, dev paper account, market closed.**

Submitted one SPY put credit spread at a deliberately impossible price — a $1
wide spread demanding $0.95 of credit, which nothing would ever pay — so the
payload shape could be tested without any chance of taking a position.

```
submitted : SPY260904P00761000 sell_to_open / SPY260904P00760000 buy_to_open
limit     : -0.95
result    : accepted, order b001d3f9-fbc8-4864-b3eb-03f67c9429f9
```

Read back from the broker before cancelling:

```
status      : ACCEPTED
order_class : MLEG
qty         : 1     filled: 0
limit_price : -0.95
legs        : 2
  SPY260904P00761000  SELL  intent=SELL_TO_OPEN  ratio=1
  SPY260904P00760000  BUY   intent=BUY_TO_OPEN   ratio=1
```

Then cancelled:

```
status after : CANCELED   filled: 0
open orders  : 0
positions    : 0
```

### What this establishes

- **The negative credit convention is real.** Alpaca accepted `-0.95` and stored
  it as `-0.95` — it did not reject it, and it did not normalise the sign. A
  credit spread genuinely is submitted as a negative limit price (GOTCHAS #7).
  This is the single most expensive thing to have got backwards, because a
  positive limit on a credit spread reads as an offer to *pay* and can fill.
- The `mleg` payload shape is accepted as constructed: no top-level symbol,
  explicit `type: limit`, string values throughout, `position_intent` per leg,
  GCD-reduced ratios.
- `position_intent` survives the round trip. The broker echoes back
  `SELL_TO_OPEN` and `BUY_TO_OPEN` rather than dropping them.
- The order carries our own `client_order_id`, so the idempotency lookup has
  something to find.
- Cancellation works on an accepted multi-leg order and leaves nothing behind.

### What it does NOT establish

- **Nothing about fills.** The order was designed not to fill. Partial fills,
  the parent/leg unit split, and the signed `filled_avg_price` are still
  unverified against real data.
- Nothing about behaviour during market hours: queueing, marketability against
  the indicative feed, or how quickly a realistic limit fills.
- Nothing about the reconciliation path under a genuine timeout. The
  lookup-before-retry logic is tested against fakes only.

## Still to prove

- [ ] A fill, and the parent versus leg reporting units on it (GOTCHAS #8).
- [ ] `order get-by-client-id` against a real order, through the CLI reconciler.
- [ ] Restart recovery on the deployed container: kill it and confirm state
      rebuilds from the mounted volume.
- [ ] The dashboard rendering in a browser. Its JavaScript parses, but has
      never executed.
