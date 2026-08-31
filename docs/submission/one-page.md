# Underwriter — AI logic, risk gates, and Alpaca infrastructure

**An autonomous agent that sells volatility insurance on liquid ETFs, and declines far more often than it writes.** Paper trading only.

Options are priced off *implied* volatility — what the market expects. What follows is *realised* volatility. Implied exceeds realised the large majority of the time, because sellers demand compensation for bearing tail risk. That gap is a structural insurance premium, and it cannot be traded with stock: it exists only in options. Underwriter ranks a fixed universe by how rich that premium is and sells defined-risk credit spreads into it.

Each cycle runs nine stages — observe, preflight, rank, regime, veto, select, risk, journal-and-submit, exit — and **any stage can stop the trade. There are 58 distinct refusal reasons across them,** every one carrying a code that reaches the audit log.

---

## AI logic

**There is exactly one place a language model touches a trade, and it can only ever remove candidates. It never adds one.**

Selling premium into a real catalyst is the classic way to be run over. High implied volatility is sometimes mispricing, and sometimes the market correctly pricing a known event — an OPEC meeting, a pending ruling, earnings for a dominant constituent. The premium ratio cannot distinguish them. Reading news and a calendar is exactly the unstructured judgement a model is good at, so it answers one question per candidate: *is there an identifiable scheduled or unfolding event that explains this elevated volatility?*

Three properties are enforced in code rather than trusted:

- **A hallucinated catalyst costs an opportunity, never money.** No model output causes a trade to happen.
- **Every failure is a veto.** Timeout, malformed JSON, missing field, empty response, exception — all decline. The failure mode of an unavailable model is that the agent trades *less*, never that it trades unguarded.
- **The model never sees our book.** The prompt carries a ticker, two volatility figures and headlines — no strike, size, price or account state. It answers a question about the world, not about our position, which also leaves a prompt injection nothing to aim at. Headlines are marked untrusted and truncated at the prompt boundary.

Six providers sit behind one interface -- Anthropic and OpenAI direct, plus DeepSeek, OpenRouter, MiniMax and Featherless, which speak the OpenAI wire format and so cost a table row rather than a client. The failure semantics are identical whichever is wired, and selection never silently falls back to another: naming a provider whose key is unset is a startup error, because which model screened a trade belongs in the audit trail.

## Risk gates

Sizing, caps, stops and exits are arithmetic on account state. **None of them consults the model** — risk management that waits on an API call is not risk management, and a test asserts the exit module imports nothing that could reach one.

| | |
|---|---|
| Risk per trade | 0.5% of equity |
| Concurrent positions | 6 |
| Aggregate open risk | 3% |
| Daily loss stop | 1.5% of **session-open** equity |
| Net delta cap | 150 share-equivalents per $100k |
| Entry cutoff | 15:00 ET |

Every applicable gate is evaluated rather than short-circuiting, so a trade refused for four reasons logs four reasons. Three design choices carry most of the weight:

**Unknown denies.** Unreadable equity, an unreadable baseline, or an uncomputable delta all refuse the trade. A gate that skips when an input is missing turns itself off precisely when we can see least — and does so silently.

**Every short put loses together.** Per-position limits are insufficient by construction, so three gates operate at different scales: a regime filter blocks all entries in a hostile tape, a correlation gate stops two positions being one bet, and an aggregate delta cap stops six of them being one bet. The regime filter includes the only forward-looking input — an inverted implied-volatility term structure, which typically appears *before* realised volatility expands.

**Exits are pure code**, five triggers, most-severe first: hard flatten, time stop, loss limit, regime break, profit target. Precedence is meaningful — a position both past its profit target and inside the mandatory flatten window exits on the window, because the target is a preference and the window is a deadline.

## Alpaca infrastructure

`alpaca-py` carries order submission; the **Alpaca CLI is the reconciler**, so confirmation of an ambiguous outcome arrives over a *different transport* than the one that failed to report it. The SDK was chosen for submission because its retry loop is ours and is switched off and verified off — it retries POSTs on 429 and 504 by default, and the documented way to disable that silently does nothing.

**Idempotency is structural.** Submitting a second time requires a proof of absence obtainable only from a lookup that positively returned *no such order*, bound to that specific order ID. "Retry without checking" is unrepresentable, not merely discouraged. If a lookup is inconclusive the agent stops and asks for a human: a missed trade costs an opportunity, a duplicate spread doubles the risk with no record of why.

Orders are journalled as **intent before submission**, so a crash between the two is recoverable. The journal is SQLite in WAL mode with `synchronous=FULL`, on a mounted volume; parent and leg fills live in separate tables because they are different quantities in different units with different signs.

Twenty platform behaviours are documented in `docs/GOTCHAS.md`, several contradicting the published documentation. The most expensive: **a multi-leg credit order takes a negative `limit_price`.** Get it backwards and an order to *collect* $1.20 becomes an offer to *pay* it — no error, plausibly filled. Verified first-hand against the live API.

## Honesty

Four sessions cannot demonstrate the loss side of a short-volatility strategy. The whole contest runs on free-tier data: IEX equities, option trades delayed fifteen minutes, and **indicative option quotes rather than real NBBO**. Paper fills simulate against those same estimates and the multi-leg fill model is undocumented, so results are reported beside a conservative shadow P&L that reprices every fill at the worse of what we got and what we asked for. **That is the number to believe.**

The edge is real but not novel — every options desk harvests this premium, and it exists as compensation for genuine tail risk rather than as free money.
