# Demo video script

Target: **under 5 minutes, under 300MB.** 16:9 is recommended, not required.

Record in one take if you can. A slightly rough single take reads as real; a
polished cut of a system that has never run reads as a mock-up, and the whole
argument here is that the thing actually works.

## Before you hit record

```bash
# One terminal, big font, dark theme. Nothing else on screen.
cd ~/projects/alpaca
clear
```

- Close anything with credentials visible. `.env` stays shut.
- Have the dashboard open in a browser tab, already loaded.
- Have the deck open in a third tab if you want to cut to a slide.
- Decide now whether you are showing live trading or dry run, and **say which
  on camera**. Do not let a viewer assume.

---

## 0:00 — 0:25 · What it is

*On camera or voiceover, dashboard visible.*

> This is Underwriter. It's an autonomous agent that sells volatility
> insurance on liquid ETFs — and it declines far more often than it writes.
>
> Options are priced off *implied* volatility, what the market expects. What
> actually happens is *realised* volatility. Implied exceeds realised most of
> the time, because option sellers are being paid to carry tail risk. That gap
> is the trade. You can't get at it with stock — it only exists in options.

**Do not** say "we found an edge". Say the premium is well documented and
structural. It is the honest framing and it survives a judge's question.

---

## 0:25 — 1:10 · One real trade

*Cut to the payoff diagram — the P&L share card, or slide 3.*

> Here's a single position, priced on a real chain.
>
> We sell the SPY 761 put and buy the 760 as a cap. We collect seventy-five
> dollars. The most we can lose, ever, is four hundred and twenty-five — and
> we know that number before we enter, because the second leg bounds it.
>
> Breakeven is 760.85. **SPY can fall one point one percent and we still keep
> every cent.** That's the whole pitch: we get paid when nothing happens.

---

## 1:10 — 2:20 · Watch it run

*Terminal. This is the heart of the video — real output, not slides.*

```bash
uv run calibrate
```

While it runs:

> This is the agent looking at the market right now. It measures realised
> volatility from price history against implied volatility from the option
> chain, and ranks the universe by how rich the premium is.

When the ranking table appears, **point at the rejections, not the candidates**:

> Four of these clear the floor. The rest are declined, and each one says why.
> That's the part I actually want to show you.

Then refresh the live Railway dashboard after the next supervised cycle. Do
not run an invented one-cycle command for the recording: `underwriter-serve`
is the real supervisor and is already running the judged service.

> That's a full cycle. It observed the book, ran preflight against the live
> account, ranked the universe, checked the market regime, and then rejected
> every candidate at contract selection — because it's outside market hours and
> the quotes are stale. It refuses to price a spread off a forty-hour-old quote.

---

## 2:20 — 3:20 · The refusals are the product

*Switch to the dashboard, refusals section.*

> Most trading dashboards lead with a P&L curve. This one leads with what the
> agent refused to do.
>
> There are fifty-eight distinct reason codes. Position caps, correlated
> exposure, a daily loss stop, stale quotes, spreads too wide, an inverted
> volatility curve, a scheduled jobs report inside the holding period.
>
> With a four-day judged window, a P&L number tells you almost nothing. These
> tell you whether the risk engineering is real.

*If you have open positions, show the book and the exit triggers here.*

---

## 3:20 — 4:05 · Where the AI actually is

> There's exactly one place a language model touches a trade, and it can only
> ever *remove* candidates. It never adds one.
>
> Selling premium into a real catalyst is how you get run over. High implied
> vol is sometimes mispricing and sometimes the market correctly pricing an
> OPEC meeting. The ratio can't tell those apart; reading the news and a
> calendar is exactly what a model is good at.
>
> So it answers one question per candidate: is there an identifiable reason
> this volatility is elevated? A hallucinated catalyst costs us an
> opportunity. It cannot cost us money. And every failure — a timeout, bad
> JSON, an empty response — is treated as a veto.

Optional, if it lands in time: show a real veto in the decision log.

---

## 4:05 — 4:40 · What's weak about it

*This is the slide that wins the argument. Do not cut it for time.*

> Two honest things.
>
> Four sessions cannot demonstrate the loss side. This is short volatility —
> many small wins and occasional larger losses. A green week doesn't validate
> it and a red week doesn't refute it.
>
> And the edge is real but not novel. Every options desk harvests this
> premium. It exists as compensation for genuine tail risk, not as free money.
> If this is good, it's good for the engineering and the discipline.
>
> Which is why the official paper P&L is reported next to a conservative
> shadow P&L. Paper fills simulate against estimated quotes, so the shadow
> number is the honest one.

---

## 4:40 — 5:00 · Close

*Dashboard or the closing slide.*

> One thousand one hundred and seven tests. Fifty-eight ways to say no. Running live
> on a paper account with a persistent audit trail of every decision it made
> and every one it refused.
>
> An underwriter that approves everything isn't an underwriter.

---

## Cutting room notes

**If you are over five minutes**, cut in this order:
1. The second terminal command (keep calibration)
2. The open-book walkthrough
3. Detail in the AI section — keep "can only remove candidates"

**Never cut:** the payoff diagram, the refusals, or the weaknesses slide.

**Do not** show `.env`, the Railway variables page, or any account ID. If a
credential appears on screen for one frame, re-record — do not trim it out and
hope.

**Say "paper trading" at least twice.** It is a hackathon rule and a judge
should never have to wonder.
