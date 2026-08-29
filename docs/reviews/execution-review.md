# Adversarial review — `src/underwriter/execution.py`

**Reviewed:** 2026-08-28
**Scope:** `src/underwriter/execution.py` (1741 lines, md5 `6f3b5acd96b69e381f8d9c1ccd77a386`),
`tests/test_execution.py` (1503 lines)
**Cross-read:** `docs/GOTCHAS.md` (all 20, especially #6, #7, #8, #9, #11, #15, #16, #17, #18),
`docs/research/orders-api-reference.md`, `docs/research/cli-reference.md`,
`src/underwriter/chain.py` (`CreditSpread`, `select_credit_vertical`), `src/underwriter/config.py`
**Baseline:** no files were modified. Every claim below was executed, not just read; reproduction
scripts are in the session scratchpad (`probe.py`, `probe2.py`).

**Moving target.** The file changed twice during the review. Line numbers and all reproductions are
against md5 `6f3b5acd…`. Two changes landed mid-review and are accounted for below: `build_adapter`
now fails closed with no SDK client instead of falling through to a CLI POST (this **closed** a
finding, recorded as **F1**), and `fetch_legs` was added using `alpaca order get --order-id --nested`
(this **closed** the `--nested` recovery gap, recorded as **F2**).

---

## Summary

| Rank | Count | Findings |
|---|---|---|
| CRITICAL | 1 | C1 no limit-price sign assertion |
| HIGH | 4 | H1 recovery lookup does not verify identity, H2 422/429 terminal on submit, H3 injected SDK client keeps retries, H4 timeout below the CLI retry envelope + no settle delay |
| MEDIUM | 4 | M1 single-shot lookup, M2 403 mislabelled, M3 same-day id collision, M4 no paper check on an injected client |
| LOW / nit | 6 | L1–L6 |
| Fixed mid-review | 2 | F1 CLI fall-through, F2 `--nested` gap |

The idempotency machinery is genuinely good. `_AbsenceProof` does what it claims, `_resubmit` cannot
be called without a proof bound to the specific order, and I could not construct any path that
submits twice without one. The failure-classification bias toward UNKNOWN is real and correct on
every response-shape case I threw at it.

The pattern in the defects is consistent and worth naming: **the module guards the transport
thoroughly and the semantics barely at all.** It knows exactly what to do when it cannot read a
response, and almost nothing about whether the response it *can* read is the right one. It will not
resubmit without proof of absence, but it accepts any order the broker hands back as proof of
presence. It validates leg count, symbol uniqueness, ratio GCD, time-in-force, cent quantisation and
client-order-id charset — and not the one field whose sign is the documented, money-losing trap.

---

## CRITICAL

### C1 — Nothing asserts the limit-price sign; a negative credit reaches the wire as a positive limit on an opening credit spread

**Where:** `src/underwriter/execution.py:339` (`validate`), `:386` (`to_limit_price`)

`to_limit_price` computes `signed = -magnitude if credit else magnitude` with no check that `amount`
is the positive magnitude its docstring promises, and `validate()` never checks that the price's sign
agrees with the legs' `position_intent`.

**Reproduction** (`probe.py`, run against the current file):

```
build_opening_order(spread(credit=-0.50), contracts=1, now=NOW)
  payload limit_price: "0.50"
  legs:                [('sell','sell_to_open'), ('buy','buy_to_open')]
  validate() says:     None
  is_credit:           False
  argv:                --limit-price=0.50

build_opening_order(spread(credit=0.42), contracts=1, credit=-0.42, now=NOW)
  limit_price: "0.42"   validate: None
```

**What breaks.** That is GOTCHAS #7's catastrophe submitted clean: an opening credit spread priced as
"I will pay $0.50 to enter this." It does not error, it is plausibly filled by whoever takes the
other side, and it is visible only as inexplicable P&L. Both the sell-to-open legs and a positive
(debit) net price go out together, and the module's own last line of defence waves it through.

**Reachability, stated honestly.** It is not reachable through the current selector path. I verified
`chain.py:599` rejects `credit is None or credit <= 0` before a `CreditSpread` is constructed, and
`select_credit_vertical` additionally enforces `min_credit_fraction_of_width = 0.15`, so a spread from
the selector always carries a positive credit. But:

- the guarantee lives in a *different module*, one import away from the code that depends on it;
- `CreditSpread` is a plain frozen dataclass that any caller can construct directly;
- `build_opening_order(credit=…)` and `build_closing_order(debit=…)` are explicit overrides that
  bypass the selector entirely — and the `credit=` docstring invites exactly the kind of caller who
  would compute one (*"a marketable-limit probe, say"*).

This is the class of guarantee that holds until someone adds a second caller.

**The reference doc asks for this guard in as many words.** `docs/research/orders-api-reference.md:212`:

> Assert the sign before every submission; refuse to submit a credit-spread open whose computed
> `limit_price` is ≥ 0.

**Fix.** Two guards, both a few lines:

1. In `validate()`, derive the expected sign from the legs and reject a mismatch. Any leg with
   `sell_to_open` present ⇒ `limit_price < 0`; any leg with `buy_to_close` present ⇒
   `limit_price > 0`. Both builders satisfy this by construction, so it costs nothing and catches
   every caller that does not.
2. In `to_limit_price`, reject `amount <= 0` — the parameter is documented as a magnitude, so a
   non-positive value is a caller bug, not an input.

---

## HIGH

### H1 — The recovery lookup never verifies that the order it found is ours

**Where:** `src/underwriter/execution.py:1626`

```python
if probe.kind is Kind.ACCEPTED and probe.order is not None:
    # reports probe.order.id / status / filled_qty / filled_avg_price as ours
```

`probe.order.client_order_id` is parsed out of the response by `_broker_order_from` (`:795`) and then
never compared against `order.client_order_id`.

**Reproduction** (`probe.py`) — a backend whose submit is ambiguous and whose lookup returns a
foreign order:

```
ok: True   order_id: SOMEONE-ELSES   status: filled
(we asked for uw-open-XLE-20260828-…, the response said client_order_id="totally-different")
```

`ok=True`, a foreign order id, a foreign `status` and a foreign `filled_avg_price` all propagate into
`OrderResult` and from there into the journal.

**The concrete scenario is not a broker bug — it is your own deterministic id.** `client_order_id`
(`:410`) collides on purpose for two identical orders on the same day; that is documented and
deliberate, with `nonce` as the escape hatch. So:

1. 10:00 — open XLE 82/80 put credit spread, qty 1, limit `-0.42`. Fills. Id is
   `uw-open-XLE-20260828-<digest>`.
2. 14:00 — the strategy wants the same structure again. Same legs, same qty, same price, same UTC
   day ⇒ **the same `client_order_id`**.
3. The POST goes out and the outcome is ambiguous (timeout, 5xx, or a 409 which `:828` routes
   straight to a lookup).
4. `_lookup` returns the **morning's already-filled order**.
5. The adapter reports `ok=True, recovered=True`, carrying the morning fill.

You now believe you hold two spreads when you hold one, and you have double-counted a fill already in
the journal. Position keeping and P&L are both wrong, and every downstream risk check — the six
concurrent-position cap, the 3% aggregate open risk cap — is computed off the wrong book.

**The module already knows it needs this join.** `fetch_legs`'s own docstring (`:1443`, added
mid-review) says: *"Parent `side` and `symbol` come back as empty strings, so a lookup response
cannot on its own say which spread it is. It must be joined against our recorded payload by
`client_order_id`."* The requirement is written down; `_via` does not do it.

**Fix.** Compare the ids at `:1626`, tolerating the one documented unknown.
`orders-api-reference.md:264` records it as UNVERIFIED whether a caller-supplied parent
`client_order_id` propagates onto the mleg parent, so: compare only when the echoed value is
non-empty, and treat a non-empty mismatch as UNKNOWN — never ACCEPTED. Separately, the strategy layer
needs an explicit rule for when it must pass `nonce`; nothing currently forces it (see M3).

### H2 — A 422 on submit is classified TERMINAL, so a duplicate-id rejection abandons a possibly-live order

**Where:** `src/underwriter/execution.py:835`, and `:837` for 429

```python
if status in (400, 422):
    return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.REJECTED, message=detail)
if status is not None and 400 <= status < 500:
    return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.API_ERROR, message=detail)
```

**Reproduction** (`probe.py`):

```
_classify_status(422, is_lookup=False) -> TERMINAL / REJECTED
_classify_status(429, is_lookup=False) -> TERMINAL / API_ERROR
```

TERMINAL means `may_fall_back=False` and, critically, **no lookup**.

**Why this is the wrong status to call terminal.** The code anticipates a duplicate as HTTP 409
(`:828`), which it correctly routes to UNKNOWN → lookup. But the research says 409 is not the code
that will arrive:

- `orders-api-reference.md:288` — *"a duplicate is most likely a 422"*
- `orders-api-reference.md:770` — the 422 bucket *"absorbs … duplicate `client_order_id`"*
- `orders-api-reference.md:286` — *"The documented `POST /v2/orders` responses are only 200, 403, 422."*

So the mitigation is aimed at a status that will probably never occur, while the status that will
occur is declared a definite no.

**The failure chain is precisely GOTCHAS #16.** The CLI binary contains retry machinery
(`isRetryable`, `doWithRetry`, `retryCount`, `Retry-After`) and whether it retries a POST could not be
verified. So:

1. CLI POSTs the order. Alpaca creates it.
2. The response is lost (connection reset, gateway hiccup).
3. The CLI retries internally. Alpaca returns 422 *"duplicate client_order_id"*.
4. We see 422 → TERMINAL/REJECTED → report failure, `may_fall_back=False`, **no lookup**.

Result: a **live, unmanaged credit spread** on the judged account. No journal entry, no exit order,
no position record, and the agent has been told the trade did not happen. It surfaces at expiry, or
when Alpaca liquidates it an hour before (GOTCHAS #10) — whichever hurts more.

429 has the same shape via the generic 4xx branch. A 429 that reaches us is typically the CLI's *last*
retry, meaning earlier attempts already went out.

This branch is the one hole in the module's own stated rule, from its docstring and GOTCHAS #16:
*"Reconcile after any submission whose outcome is not a clean success."* A 422 is not a clean success.

**Fix.** On a submit (not a lookup), 422 and 429 should be UNKNOWN → lookup. To avoid pointlessly
resubmitting a genuinely malformed order, treat a 422 whose subsequent lookup proves ABSENT as
terminal-rejected rather than as a licence to resubmit — the lookup result decides, not the status
code. Narrower alternative if you want to keep 422 mostly terminal: pattern-match the 422 message for
duplicate/uniqueness and route only that to UNKNOWN. Note `orders-api-reference.md:791` documents the
`code` structure (`42210000`), so the error body is discriminable.

### H3 — `disable_automatic_retries` is applied on only one construction path; an injected SDK client keeps alpaca-py's retries

**Where:** `src/underwriter/execution.py:1140` (the function), `:1181` (`paper_trading_client`, the
only caller), `:1199` (`SdkBackend`, does not call it), `:1682` (`build_adapter`, does not call it)

**Reproduction** (`probe2.py`):

```
client._retry after SdkBackend(client=cl).submit(order): 3
client._retry after build_adapter(sdk_client=cl):        3
```

**What breaks.** The module docstring asserts retries are *"switched off and verified off
(`disable_automatic_retries`), so that risk is eliminated rather than caught"*, and `build_adapter`'s
docstring now rests the entire "only the SDK may submit" argument on that guarantee. Both are true
only if the caller happened to obtain the client from `paper_trading_client`. Any other
`TradingClient(paper=True)` handed to `build_adapter(sdk_client=…)` retries POSTs on 429 **and 504** —
and as `:1058` says in the module's own words, *"504 is the dangerous code: a gateway timeout on an
order submission means the request very possibly reached the order system, and retrying it is
precisely the double-submit this module exists to prevent."*

This matters more now than it did an hour ago. With F1 landed, the SDK is the *only* submission
transport by default. The fallback that made the CLI risky has been removed, and the guarantee that
replaced it is not enforced at the boundary that carries the POST.

**Fix.** Call `disable_automatic_retries(self.client)` from `SdkBackend.__post_init__`. This works on
a frozen dataclass — it mutates the client, not the backend — and makes it impossible to hold a
`SdkBackend` whose client can retry. Pair it with M4 in the same hook.

### H4 — The 45s subprocess timeout sits below the CLI's own retry envelope, and the absence lookup runs with no settle delay

**Where:** `src/underwriter/execution.py:101` (`DEFAULT_TIMEOUT_SECONDS = 45.0`), `:1497` (`_lookup`,
called immediately), `:1337` (`_resubmit`)

**The arithmetic.** The CLI's `--timeout` defaults to **30s per request** and it retries up to three
times (`cli-reference.md:993`). Worst-case wall clock for one `order submit` is therefore ~90s plus
backoff. Our subprocess bound is 45s. The comment at `:96` reasons its way to exactly this problem —
*"it retries 429 and 5xx up to three times, so the wall clock for one submit can exceed a single
request"* — and then picks a number smaller than the worst case. `cli-reference.md:993` explicitly
recommends the opposite: *"set your own subprocess timeout well above it (e.g. 120s)."*

**What breaks.** On any 429 or 5xx the subprocess is killed **mid-retry, with a POST possibly in
flight**. Killing the client does not un-send the request: the bytes are already at Alpaca, and the
server processes them whether or not anyone is listening for the response.

That turns the next line into a race:

1. t=44.9s — the CLI's retry POST is written.
2. t=45.0s — `subprocess.run` raises `TimeoutExpired`; Python kills the process; the socket closes.
3. t=45.2s — Alpaca creates the order.
4. t=45.1s→45.6s — `_lookup` spawns a fresh `alpaca order get-by-client-id` subprocess (process
   start + TLS + round trip). It can legitimately answer **404**.
5. A single 404 is treated as durable positive proof of absence (`_classify_status:826`, `is_lookup`
   branch) and authorises `_resubmit`.

Two live spreads sharing one `client_order_id`. And `get-by-client-id` returns **one** order, so the
duplicate is undetectable afterwards — which is the precise failure mode `build_adapter`'s new
docstring says the whole design exists to prevent.

**Honesty about likelihood.** The window is narrow and Alpaca's create→queryable latency is
undocumented, so I cannot call this likely. But the *triggering event* — a subprocess kill while the
CLI is mid-retry — is made routine rather than exceptional by the 45s choice. An absence proof is a
claim about a moment in time, and the code treats it as durable.

**Fix, cheapest first:**

1. Pass `--timeout 10` to the CLI on every invocation so its full retry envelope (~30–35s) fits
   comfortably inside our 45s bound. A subprocess timeout then means the binary hung, not that we
   interrupted an in-flight request. This is one argv element and it converts the race from expected
   to exceptional.
2. Add a settle delay (1–2s) before an absence lookup that follows a TIMEOUT specifically.
3. Consider requiring two ABSENT answers a second apart before `_resubmit` — the read is idempotent
   and free.

---

## MEDIUM

### M1 — A single failed lookup permanently ends recovery

**Where:** `src/underwriter/execution.py:1497` (`_lookup`)

`_lookup` escalates across *transports* (reconciler, then submitter) but never retries a transport.
If both answer UNKNOWN, `_via` returns `UNKNOWN_OUTCOME` with `may_fall_back=False` and the order is
abandoned for good.

`cli-reference.md:431` recommends the opposite, and it is right to: *"exit 1 with any other status →
transport/API problem; **retry the lookup**, not the submit."* The lookup is an idempotent GET. It
costs one API call and cannot create anything.

**Scenario:** the submit times out; the reconciler's lookup catches a transient 500; the SDK client
happens to be the submitter and returns the same. Nothing is resubmitted (correct) and nothing is
ever checked again (not correct) — a possibly-live spread is left with a hand-reconcile message.

**Fix:** retry the lookup 2–3 times with a short backoff before concluding UNKNOWN. The bias toward
not-resubmitting is right; giving up after one read is not the only way to express it.

### M2 — 403 is reported as `Reason.AUTH` when it means insufficient buying power

**Where:** `src/underwriter/execution.py:822`

```python
if status in (401, 403):
    return BackendOutcome(kind=Kind.TERMINAL, reason=Reason.AUTH, message=detail)
```

`orders-api-reference.md:755` is explicit: 403 = *"Forbidden — Buying power or shares is not
sufficient."* `:766` calls it *"the credit-spread agent's most likely rejection"*, and `:821`
prescribes different handling from auth: *"403 = capital problem → back off, don't retry."*

The classification (TERMINAL) is correct. The displayed reason is wrong, and this module's stated
contract is that every failure carries a *displayable* reason. Mid-session an operator reads `auth`,
goes looking at credentials, and does not look at buying power — which is the thing that is actually
wrong, and which an insufficient-options-level rejection (`orders-api-reference.md:807`, *"Likely
403"*) also lands on.

**Fix:** split out `Reason.INSUFFICIENT_BUYING_POWER` for 403 and keep `AUTH` for 401.

### M3 — Same-day identical re-entry is impossible without a nonce, and fails confusingly

**Where:** `src/underwriter/execution.py:410` (`client_order_id`)

Deterministic ids are the right call and the docstring defends them well. The consequence is that a
legitimate second identical spread on the same UTC day collides, most likely draws a 422, and — per
H2 — is reported as `rejected`, indistinguishable from a malformed order. Combined with H1, a 409
instead produces a phantom success.

The `nonce` escape hatch exists, but nothing in the module obliges a caller to use it and no test
exercises a same-day re-entry.

**Fix:** this is a strategy-layer contract, not an execution bug — but it belongs written down. Either
the sizing layer passes a nonce derived from the position count, or the entry logic must guarantee
one entry per structure per day. Say which, in the `client_order_id` docstring.

*(Verified fine while here: the UTC day bucket cannot flip mid-session. US regular trading hours are
14:30–21:00 UTC, entirely inside one UTC day, so no order changes identity across the session.)*

### M4 — Nothing verifies an injected SDK client is a paper client

**Where:** `src/underwriter/execution.py:1199` (`SdkBackend`), `:1181` (`paper_trading_client`),
`:1741` (`TRADING_HOST`, defined and never read)

`paper_trading_client` hardcodes `paper=True` as a literal, correctly. But `SdkBackend(client=…)`
accepts any object satisfying `TradingApiLike` — two methods, `post` and `get`. `assert_paper_only()`
(`:191`) reads only `ALPACA_LIVE_TRADE`, which **alpaca-py does not consult at all**. So on the SDK
path — now the default and only submission transport — there is *zero* runtime paper enforcement.
`SdkBackend.submit` calls `assert_paper_only()` at `:1130`, but on this path that call checks an
environment variable no participant reads.

The module docstring's *"A live route is not disabled here; it does not exist"* holds for the CLI. I
confirmed it there (see the paper-only audit below). It does not hold for the SDK.

`TRADING_HOST: Final = PAPER_TRADING_HOST` at `:1741` is defined, commented as *"the only host this
code path can reach"*, and never read by anything. It is exactly the check that is not performed.

**Fix:** in `SdkBackend.__post_init__` (same hook as H3), assert the client's base URL starts with
`PAPER_TRADING_HOST`, raising `LiveTradingBlocked` otherwise. Then `TRADING_HOST` earns its place.

---

## LOW / nit

### L1 — `test_there_is_no_way_to_configure_the_sign` asserts nothing

**Where:** `tests/test_execution.py:301`

```python
def test_there_is_no_way_to_configure_the_sign(self) -> None:
    import underwriter.execution as execution
    assert not hasattr(execution, "LimitPriceConvention")
```

This tests that a name the module has never had is absent. It passes regardless of whether the sign
logic is correct, and it would keep passing if someone added a sign switch under any other name. It
is the one test in the suite whose title claims the property that C1 shows is unenforced.

**Fix:** replace it with the C1 guard's test — that an opening order with a positive limit price is
refused by `validate()`.

### L2 — Stale reference to a class that does not exist

**Where:** `src/underwriter/execution.py`, `MultiLegOrder` docstring

*"`limit_price` is signed per `LimitPriceConvention`"* — there is no such class, and L1 exists to
assert there never is. Point the reader at the module-level comment block instead.

### L3 — A test comment contradicts the code's own docstring at the sign

**Where:** `tests/test_execution.py:311` vs `src/underwriter/execution.py:386`

The test comment says asking 0.42 for a modelled 0.4278 means *"we ask for slightly less than
modelled, so the fill can only beat the model."* `to_limit_price`'s docstring says the opposite and is
correct: *"makes the realised economics no better than the modelled ones — never better."* Filling at
0.42 against a model of 0.4278 is 0.78 cents *worse*, not better.

Harmless as executed, but this is confusion in a comment at the exact place where confusion is
expensive.

### L4 — `--limit-price=-0.42` has never been exercised against the real binary

**Where:** `src/underwriter/execution.py:917` (`submit_argv`)

The `--flag=value` form is the correct defence against `-0.42` being parsed as a flag, and the
docstring says so. But `cli-reference.md:1.3` tested only `--limit-price 1.00` — space-form and
positive — and `cli-reference.md:1.6` records the sign convention as UNKNOWN at the CLI layer. This is
an untested assumption sitting on the money path.

**Fix:** one `--dry-run` invocation. It makes no HTTP call, needs only credentials, and confirms both
that the `=` form parses and that the negative survives into the serialised body.

### L5 — GOTCHAS #18 is stale and contradicts the CLI reference

**Where:** `docs/GOTCHAS.md:245` vs `docs/research/cli-reference.md:752`

GOTCHAS says *"Pass `--output json` explicitly on every invocation rather than inheriting it."* The
CLI reference says *"There is no `--output` flag. The docs' `--output json` does not exist."*

The code does the right thing — it pins `ALPACA_OUTPUT=json` in the child environment (`:210`) and
never passes the non-existent flag. **Fix the gotcha, not the code.** Following GOTCHAS #18 literally
would make every CLI invocation fail with `unknown flag`.

### L6 — A sub-cent credit produces a confusing rejection

**Where:** `src/underwriter/execution.py:386`, `:372`

`to_limit_price(0.005, credit=True)` → `Decimal("-0.00")`, which `validate` then rejects as
*"limit_price of zero is neither a debit nor a credit"*. Correct outcome, misleading message — the
input was not zero. A sub-cent credit is a real (if useless) input; naming it as such would be
clearer.

---

## Fixed during the review

### F1 — `build_adapter()` with no SDK client used to submit via the CLI (now fails closed)

In the version I first read, `build_adapter` defaulted to `primary=Backend.SDK` with
`sdk_client=None`, and the adapter's `submit` loop treated an unavailable primary as a `continue` —
so the POST fell through to the CLI fallback. The docstring at the time claimed the opposite:
*"Pass `sdk_client=None` and `primary=Backend.SDK` to make submission fail closed instead of falling
through to the CLI."* Verified then:

```
ok: True   backend: cli   order_id: ord-1
CLI invoked? True  ['alpaca','order','submit','--order-class','mleg','--qty', …]
```

The current version removes the fallback entirely unless
`i_accept_undetectable_duplicate_orders_from_the_cli=True` is passed. Re-verified against md5
`6f3b5acd…`:

```
ok: False   backend: sdk   order_id: None
CLI invoked? False
```

**Closed.** The parameter name is a good piece of design — it cannot be enabled by accident or read
in a diff without understanding the cost. Note that H3 and M4 are what now hold up the argument this
change rests on.

### F2 — `--nested` recovery gap (now closed by `fetch_legs`)

`order get-by-client-id` has no `--nested` flag (`cli-reference.md:434`), so the recovery lookup
returns the parent only. `fetch_legs` (`:990` CLI, `:1270` SDK, `:1443` adapter) now closes this with
`alpaca order get --order-id <id> --nested`, which does support the flag (`cli-reference.md:2.2`).

**Assessment of the residual risk: low, and it was always low for this module.** `execution.py` reads
only `id`, `status`, `filled_qty` and `filled_avg_price` — all parent fields, and the parent's
`filled_avg_price` is the signed net per spread, which is exactly the number a defined-risk vertical
needs. The real cost of missing leg detail was that per-leg fills are the only empirical way to check
a filled parent's signed net against the sum of its legs — i.e. the only way to *detect* a sign error
after the fact — and they were missing on precisely the recovery path where an order arrived without
you seeing the response. That is now retrievable. The `fetch_legs` docstring's own analysis of what
the parent can and cannot tell you is correct on all six points.

---

## Explicit audits requested in the brief

### 1. The `ROUND_CEILING` claim — verdict: **the claim is correct, in both directions**

The docstring at `:386` claims: *"Rounding is `ROUND_CEILING` on the signed value in both directions,
which is one rule that happens to be conservative twice over."*

**Worked by hand.** `ROUND_CEILING` rounds toward +∞ — *not* toward larger magnitude. That asymmetry
is what makes one rule work for both cases.

| Case | Magnitude | Signed | Ceiling to cent | Effect |
|---|---|---|---|---|
| Credit 0.4278 | 0.4278 | −0.4278 | **−0.42** | we demand 42¢ instead of 42.78¢ — **0.78¢ less premium** |
| Credit 0.4212 | 0.4212 | −0.4212 | **−0.42** | we demand 42¢ instead of 42.12¢ — 0.12¢ less |
| Debit 0.4278 | 0.4278 | +0.4278 | **0.43** | we offer 43¢ instead of 42.78¢ — 0.22¢ more to close |
| Debit 0.4212 | 0.4212 | +0.4212 | **0.43** | we offer 43¢ instead of 42.12¢ — **0.88¢ more to close** |

**The code produces exactly this** (`probe.py`, executed):

```
amount=0.4278 credit=True  -> -0.42
amount=0.4212 credit=True  -> -0.42
amount=0.4278 credit=False ->  0.43
amount=0.4212 credit=False ->  0.43
```

**Is "conservative in both directions" the right description?** Yes, on both readings that matter:

- **Fill-friendliness.** Opening, we ask for *less* premium than modelled, so we are easier to fill.
  Closing, we offer *more* than modelled, so we are easier to fill. Both lean toward getting done,
  which is what you want on a defined-risk vertical where the risk of not being filled on an exit is
  larger than a cent of edge.
- **Backtest honesty.** Realised economics are never *better* than modelled. Opening collects ≤ the
  modelled credit; closing pays ≥ the modelled debit. A fill at our limit can only understate
  performance, never flatter it.

The docstring's own summary — *"never better, which is the direction that keeps the backtest
honest"* — is precisely right. Note L3: a test comment at `tests/test_execution.py:311` states the
inverse (*"the fill can only beat the model"*) and is wrong; the docstring is the correct statement.

**One caveat, not a defect.** The magnitude-effect is asymmetric across the cent boundary: a credit
loses up to 0.99¢ and a debit gains up to 0.99¢. On a 0.42 credit that is up to ~2.3% of the premium
per spread. That is the correct trade to make, but it is worth knowing it is not free, and it argues
for modelling credits at the cent rather than to four decimals when computing expected P&L.

### 2. Failure-classification audit — no genuinely-unknown outcome is misclassified as accepted; one is misclassified as terminal

The rule to check: nothing ambiguous may be called ACCEPTED (catastrophic — we would report a fill
that may not exist and never reconcile), and nothing ambiguous should be called TERMINAL (abandons a
possibly-live order).

**The four response shapes named in the brief. All four handled correctly:**

| Case | Path | Result | Verdict |
|---|---|---|---|
| exit 0, empty stdout | `_loads("")` → `None` → `_broker_order_from(None)` → `None` | `UNKNOWN / MALFORMED_RESPONSE` → lookup | correct |
| exit 0, valid JSON, no order id | `_broker_order_from` requires `id` **and** `client_order_id` to be `str` (`:795`) → `None` | `UNKNOWN / MALFORMED_RESPONSE` → lookup | correct |
| HTTP 200 with an error body | CLI exits 0, stdout is `{"error": …}`, no `id` → `None` | `UNKNOWN / MALFORMED_RESPONSE` → lookup | correct |
| truncated / partially written JSON | `json.JSONDecodeError` caught in `_loads` (`:995`) → `None` | `UNKNOWN / MALFORMED_RESPONSE` → lookup | correct |

The comment at the exit-0 branch is the right instinct: *"Exit 0 means it very probably worked, but
we cannot read the order. Do not call that a failure and do not retry it."*

**Full branch audit of `_classify_status` (`:813`) and `_interpret` (`:1032`):**

| Input | Classification | Verdict |
|---|---|---|
| subprocess timeout | UNKNOWN / TIMEOUT | correct |
| exit 0 + parseable order | ACCEPTED | correct |
| exit 0 + unparseable | UNKNOWN / MALFORMED | correct |
| exit 2 | TERMINAL / AUTH | correct — exit 2 is HTTP 401 only (`cli-reference.md:36`) |
| exit 1, `status: 0`, `"authentication required"` | TERMINAL / AUTH | correct, and the reasoning at `:1002` is exactly right: this is the one status-0 error provably from *before* any request |
| exit 1, `status: 0`, any other error | UNKNOWN / API_ERROR | correct — this covers local flag-parse failures (`--legs: …`, `unknown flag: …`), which are safe but indistinguishable from a dropped connection |
| exit 1, unstructured stderr | UNKNOWN / API_ERROR | correct |
| 401 | TERMINAL / AUTH | correct |
| 403 | TERMINAL / **AUTH** | status correct, **reason wrong** — see M2 |
| 404 on lookup | ABSENT | correct, and matches the verified CLI miss shape (`cli-reference.md:410`) |
| 404 on submit | TERMINAL / API_ERROR | correct — wrong route, no order created |
| 409 | UNKNOWN / DUPLICATE → lookup | correct in principle, but see H2: 409 is not in the documented response set |
| 400 | TERMINAL / REJECTED | correct — malformed payload |
| **422** | **TERMINAL / REJECTED** | **wrong — H2.** The most likely duplicate-id response, abandoned without a lookup |
| **429** | **TERMINAL / API_ERROR** | **wrong — H2.** Usually the CLI's last internal retry |
| other 4xx | TERMINAL / API_ERROR | acceptable |
| 5xx | UNKNOWN / API_ERROR | correct — the request may have been applied |
| unreadable status | UNKNOWN / API_ERROR | correct |
| SDK exception, class name contains "Timeout" | UNKNOWN / TIMEOUT | correct |
| SDK exception, no `status_code` | UNKNOWN / API_ERROR | correct |

**Conclusion.** Nothing ambiguous is misclassified as ACCEPTED — the catastrophic direction is clean,
and the "bias toward UNKNOWN" claim holds on every response-shape case. The defect is in the other
direction and is narrow: **422 and 429 on a submit are called terminal when they are the two statuses
most likely to be reporting on an order that already exists.** That is H2.

*(One structural note on `_interpret:1064`: `body = _loads(stderr) or _loads(stdout)`. A stderr body
that parses to a falsy JSON value — `0`, `[]`, `""`, `false` — falls through to stdout. Harmless in
practice, since none of those shapes is a CLI error body, but `is not None` would be the exact test.)*

### 3. Paper-only audit — the CLI path is airtight; the SDK path has no enforcement at all

**Can any path reach the live API?**

**CLI: no.** `cli-reference.md:1003` enumerates every `ALPACA_*` variable the binary reads, recovered
from its own string table: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_LIVE_TRADE`,
`ALPACA_PROFILE`, `ALPACA_OUTPUT`, `ALPACA_CONFIG_DIR`, `ALPACA_QUIET`, `ALPACA_DEBUG`,
`ALPACA_VERBOSE`, `ALPACA_TRACE`. Of these, **only `ALPACA_LIVE_TRADE` selects the live base URL**
(*"`true` (case-insensitive) → live base URL; anything else → paper"*). A profile carries credentials,
not a base URL; `ALPACA_CONFIG_DIR` relocates config, not the endpoint. There is no base-URL override
and no `--live` flag on `order submit` (`cli-reference.md:80`, verified: `unknown flag: --live`). So
pinning `ALPACA_LIVE_TRADE=false` genuinely closes the only door.

**SDK: no enforcement.** `paper_trading_client` hardcodes `paper=True`, but any client can be injected
and nothing checks it — see M4. `assert_paper_only()` is called in `SdkBackend.submit` but reads an
environment variable alpaca-py never consults, so on this path it is decorative.

**Does the `ALPACA_LIVE_TRADE=false` pin survive an inherited environment?** **Yes.** `paper_environment`
(`:210`):

```python
env = dict(os.environ)      # inherit everything
if extra:
    env.update(extra)       # caller additions
env[LIVE_TRADE_ENV_VAR] = "false"   # pinned LAST
env[OUTPUT_ENV_VAR] = "json"        # pinned LAST
```

The pins are applied **after** both the inherited environment and `env_extra`, so neither can override
them. The tests confirm both directions (`tests/test_execution.py:239-243`): a parent with
`ALPACA_LIVE_TRADE=true` and an explicit `extra={"ALPACA_LIVE_TRADE": "true"}` both come back
`"false"`. The child process receives exactly this dict via `env=dict(env)` in `subprocess_runner`, so
there is no path by which the parent's value reaches the CLI.

Two further points, both fine:

- **`assert_paper_only` is stricter than the CLI**, deliberately and in the safe direction. The CLI
  treats anything other than the exact string `true` as paper; we block on anything outside
  `{"", "false", "0", "no", "off"}`. So `ALPACA_LIVE_TRADE=1` leaves the CLI on paper but stops us.
  Divergent, documented, and stricter is right.
- **`.env` is not a hole.** pydantic-settings reads `.env` without mutating `os.environ`, so a
  `.env` containing `ALPACA_LIVE_TRADE=true` would not be seen by `assert_paper_only` (which reads
  `os.environ` only) — but `Settings._refuse_live_trading` (`config.py:157`) raises
  `LiveTradingBlocked` at startup, and the value never reaches the child env either. Layered
  correctly, though it is worth knowing that `assert_paper_only` alone is not the complete check;
  `Settings` is.

One thing this does **not** protect against, worth stating: `assert_paper_only` guards the *process
environment*, not the *credentials*. Live API keys in `ALPACA_API_KEY` with `ALPACA_LIVE_TRADE=false`
would send live credentials to the paper host — which fails, safely. The reverse cannot happen. The
`.env` note about keeping the competition account's credentials separate until the judged run is the
real control here, and it is a process control, not a code one.

### 4. Payload compared field by field against `orders-api-reference.md`

Generated payload (`as_payload`, verified by execution for a 3-lot XLE 82/80 put credit spread at a
modelled 0.4278 credit):

```json
{
  "order_class": "mleg",
  "qty": "3",
  "type": "limit",
  "limit_price": "-0.42",
  "time_in_force": "day",
  "client_order_id": "uw-open-XLE-20260828-<digest>",
  "legs": [
    {"symbol": "XLE260918P00082000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "XLE260918P00080000", "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"}
  ]
}
```

| Requirement | Source | Code | Verdict |
|---|---|---|---|
| `order_class: "mleg"` | `orders-api-ref` §1 CONFIRMED table | `ORDER_CLASS` constant, no other value reachable | PASS |
| no top-level `symbol` | *"required for all order classes except for `mleg`"* | absent from `as_payload`; `submit_argv` never emits `--symbol` (asserted at `tests:672`) | PASS |
| no top-level `side` | *"Required for all order classes except for mleg"* | absent | PASS |
| no top-level `position_intent` | per-leg for mleg | absent from the parent, present on every leg | PASS |
| all scalars as strings | *"schema types are `string` throughout"* | `str(qty)`, `str(ratio_qty)`, `format(price, "f")`; asserted at `tests:366` | PASS |
| `type: "limit"` explicit | GOTCHAS #6 — CLI defaults to `market` | `ORDER_TYPE` constant; `--type limit` always in argv | PASS |
| `time_in_force` in `{day, gtc}` | §3, options-only enum | `VALID_TIME_IN_FORCE`, validated | PASS |
| 2–4 legs | `maxItems: 4`; a vertical needs 2 | `MIN_LEGS`/`MAX_LEGS`, validated | PASS |
| unique leg symbols | implied by the coverage rule | validated explicitly | PASS |
| `ratio_qty` GCD-reduced | §1.2 verbatim: *"GCD … must be 1"* | validated (`math.gcd(*ratios) != 1` → reject); `reduce_ratios` available to callers | PASS |
| `position_intent` per leg | `MLegOrderLeg.position_intent` | always sent — correctly, since the field is schema-*optional* and omitting it lets the broker infer intent from existing positions | PASS |
| limit price sign | §2: credit negative, close positive | correct for both builders — **but unasserted, see C1** | PASS with C1 |
| cent quantisation | §1.2 sub-penny note: *"always round net credit to 2 decimals"* | `exponent != -2` → reject | PASS |
| `client_order_id` ≤ 128 chars | §4 | validated; generated ids are ~40 chars | PASS |

**No discrepancies found.** The payload matches the constructed put-credit-spread example at
`orders-api-reference.md:1.2` field for field. Key ordering differs from the doc example, which is
irrelevant for JSON; the fixed key order in `as_payload`/`as_payload` on legs exists to make the
`client_order_id` digest reproducible, which is a good reason.

Two spec rules are **not** locally validated, correctly so: the coverage rule (*"all legs covered
within the same MLeg order"*) and the no-equity-legs rule. A 2-leg vertical satisfies both by
construction, and neither is checkable without the chain.

One thing the CLI adds that we do not control: `"advanced_instructions": {}` is *always* present in
the CLI's serialised body (`cli-reference.md:157`) even when never passed, and whether the API
tolerates it on every order type is UNVERIFIED. Not our bug; worth knowing if a submission is
rejected for a field we never sent. The SDK path does not have this.

### 5. Tests — what would still pass if the logic were wrong, and what is not tested

The suite is strong: 130+ tests, no network, no credentials, no subprocess, scripted fakes for both
transports, and a fake CLI that *raises on an unscripted call* — which is the right design, because
an unexpected extra submission fails the test rather than passing silently. Several tests count
submissions explicitly, which is exactly where the money is.

**Assertions that would still pass if the logic were wrong:**

1. **`test_there_is_no_way_to_configure_the_sign` (`:301`) — passes unconditionally.** See L1. It
   asserts the absence of a name that never existed. The sign logic could be inverted and this test
   would be green.
2. **`test_the_default_wiring_puts_the_post_on_the_sdk` (`:1036`) asserts wiring shape, not
   behaviour.** It checks `built.primary.name is Backend.SDK` and that a fallback exists. It does not
   submit anything. This is precisely why F1 — a default construction that routed the POST to the CLI
   — was not caught by the suite: the shape was right and the behaviour was wrong.
3. **`test_a_rejected_order_is_terminal` (`:750`) enshrines H2.** It asserts that a 422 is terminal
   and never retried. That is currently the implementation and, per `orders-api-reference.md:288`,
   the wrong behaviour for the duplicate case. A test that pins a defect is worse than no test,
   because it makes the fix look like a regression.
4. **`test_a_403_is_read_as_auth` (`:758`) enshrines M2.** Same shape: it asserts the mislabelling.
5. **`test_non_finite_input_survives_to_validation` (`:318`)** asserts only `not price.is_finite()`.
   It would pass for `+inf`, `-inf` or NaN alike, so it does not pin the sign behaviour for a
   non-finite input.

**What is not tested and should be:**

| Missing case | Why it matters |
|---|---|
| a negative `credit`/`debit` reaching `to_limit_price` | **C1.** No test submits an opening order with a positive limit price and expects a refusal. This is the single highest-value missing test in the file. |
| lookup returns a *different* `client_order_id` | **H1.** No test asserts the recovery lookup verifies identity. |
| lookup returns *our own earlier* order (same-day id collision) | **H1/M3.** The phantom-second-position scenario is entirely uncovered. |
| a 422 whose message says "duplicate" | **H2.** No test distinguishes a malformed-order 422 from a duplicate-id 422. |
| `SdkBackend` with an injected client that has retries enabled | **H3.** `TestRetryDisabling` covers `disable_automatic_retries` thoroughly and `paper_trading_client` calls it — but nothing tests that a client reaching `SdkBackend` has been through it. |
| `SdkBackend` with a non-paper client | **M4.** No test asserts a live-configured client is refused. |
| a lookup that fails transiently then succeeds | **M1.** Nothing covers lookup retry, because there is none. |
| timeout → 404 → resubmit → the order later turns out to exist | **H4.** The race is untestable at unit level, but a test asserting a settle delay / double-confirmation would pin the mitigation once added. |
| a same-day re-entry with and without `nonce` | **M3.** `test_a_nonce_deliberately_permits_a_second_identical_position` (`:489`) covers the id differing; nothing covers the *submission* path for a colliding id. |
| `time_in_force="gtc"` surviving a full adapter round trip | covered at argv level (`:395`); not through `submit`. Minor. |

**Tests that are genuinely good and worth keeping as-is:** `test_the_lookup_happens_before_any_second_submission`
(`:861`, asserts ordering via the `on_call` sink, not just counts), `test_timeout_then_a_failed_lookup_submits_nothing_further`
(`:839`), `test_proof_for_one_order_cannot_authorise_another` (`:943`), `test_both_lookups_failing_still_refuses_to_resubmit`
(`:1113`), and the byte-exact payload and argv assertions (`:324`, `:649`) — pinning the payload
byte for byte rather than field by field is the right call for a body whose every field is a
documented trap.

---

## Attacked and found sound

Stated explicitly, because "I checked and it is fine" is worth as much as a finding:

- **Idempotency plumbing.** `_AbsenceProof` can only be obtained from `from_lookup`, which returns
  `None` unless the outcome is positively `ABSENT`. `_resubmit` requires one and raises `ValueError`
  on an id mismatch (`:1337`). I could not construct any path that submits twice without a proof
  bound to that specific order. Worst case measured (every submit times out, every lookup 404s,
  CLI primary with an SDK fallback): `SUBMIT, lookup, SUBMIT, lookup, lookup, lookup` — exactly 2
  CLI POSTs, each preceded by a positive absence proof, then escalation. The holes are in *what
  counts as proof* (H4) and in *what skips the lookup entirely* (H2), not in the proof plumbing.
- **Timeout → lookup times out** → `proof is None` → refuses, `may_fall_back=False`, hand-reconcile
  message naming the `client_order_id`. Correct.
- **Timeout → lookup 404s → resubmit also times out** → second lookup; ABSENT again → attempt limit
  reached → `ok=False, may_fall_back=True`. The fall-through is proof-guarded. Correct.
- **`--quiet` does not suppress data.** I initially suspected it would break every response parse;
  `cli-reference.md:835` verifies it suppresses only warnings, hints and colour, and *"does not
  suppress error JSON on stderr"* — a 404 with `--quiet` printed the full error object. Fine.
- **`--csv` and `--jq` are never passed**, and `ALPACA_OUTPUT` is pinned rather than inherited —
  correctly avoiding the empty-stdout-exit-0 trap of GOTCHAS #18.
- **Local CLI flag errors are safe.** A renamed flag in this alpha-preview binary produces
  `{"error": "--legs: …", "status": 0}` and exit 1 → UNKNOWN → lookup → 404 → ABSENT → resubmit →
  same error → escalate to the SDK. Two wasted attempts, correct outcome, no duplicate.
- **The UTC day bucket cannot flip mid-session** (RTH is 14:30–21:00 UTC), so a deterministic id is
  stable across a trading day.
- **`validate()` correctly enforces** leg count, non-empty and unique symbols, `ratio_qty >= 1`,
  GCD 1, `qty >= 1`, time-in-force membership, finiteness, non-zero, cent quantisation, and
  client-order-id length and charset. Every one of these is a rule the broker enforces too.
- **`_as_decimal` returns `None`, never `0`, for an unparseable or missing fill field** (`:726`),
  with the right reasoning: *"Zero and 'not reported' mean opposite things on a fill."*
- **The parent/leg unit trap (GOTCHAS #8) is documented and respected** — `BrokerOrder` reads parent
  fields only, and never reads the parent's `side`/`symbol`, which come back empty.

---

## Recommended order of work

1. **C1** — sign assertion in `validate()` and a non-positive guard in `to_limit_price`. Few lines,
   removes the catastrophic direction.
2. **H1** — identity check on the recovery lookup. Few lines, removes the phantom-position direction.
3. **H2** — route 422/429 on submit through a lookup. Removes the abandoned-live-order direction.
4. **H3 + M4** — one `SdkBackend.__post_init__` that disables retries and asserts the paper host.
   Makes the current default wiring's argument actually true.
5. **H4** — pass `--timeout 10` to the CLI; add a settle delay before a post-timeout absence lookup.
6. **L4** — one `--dry-run` with a negative limit price, before the judged run. Free, and it closes
   the last untested assumption on the money path.
7. **M1, M2, M3, L1–L3, L5, L6** as time allows. L5 is a docs fix in `GOTCHAS.md`, not code.

**Before the judged run, regardless of the above:** submit one far-OTM put credit spread on paper with
a deliberately unfillable negative limit price and confirm it is accepted (`new`/`accepted`) rather
than 422-rejected, as `orders-api-reference.md:216` recommends. The sign convention is well evidenced
— the OAS text is unambiguous, the 3:1 worked example's parent `filled_avg_price` of 1.28 equals the
signed leg net, and the Level 3 cost-basis example states a $5 credit *"becomes -$5"* — but it has
never been confirmed against a real fill, and this test costs nothing.

---

## Snapshot note

At the time of writing, `tests/test_execution.py` has one failing test —
`TestFailsClosedWithoutAnSdkClient::test_an_sdk_that_raises_on_submit_still_never_reaches_the_cli` —
arising from the in-progress F1 change, not from anything in this review. Everything else passes.
