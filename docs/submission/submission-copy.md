# Copy-ready submission fields

## Title

Underwriter

## Tagline

Volatility insurance, governed by evidence.

## Short description

Underwriter is an autonomous, paper-only options agent that sells defined-risk
volatility insurance on liquid ETFs and records every reason it declines a trade.

## Full description

Underwriter ranks liquid ETFs by implied volatility relative to realised volatility,
then sells bounded-risk credit spreads only when deterministic market, liquidity, and
portfolio gates all agree. A language model has one deliberately asymmetric role: it
may veto elevated volatility explained by a catalyst, but it can never add a candidate,
size a trade, set a price, or control an exit. Unknown inputs fail closed.

The execution path journals intent before submission, reconciles ambiguous outcomes over
a second Alpaca transport, and restores open state from SQLite after a process restart.
The public dashboard is read-only and reports every refusal, official paper P&L, and a
more conservative shadow P&L. The system is backed by 1,107 passing tests plus live
multi-leg order/cancel and Railway volume-remount probes.

## Suggested tags

`AI agents` · `Alpaca` · `options` · `paper trading` · `risk management` · `fintech` ·
`Python` · `FastAPI` · `Railway` · `SQLite`

## Portal-only fields

Never replace these placeholders in a committed file:

- App URL: `<PUBLIC_DASHBOARD_URL>`
- Repository URL: `<PUBLIC_REPOSITORY_URL>`
- Demo video URL: `<PUBLIC_VIDEO_URL>`
- Competition paper account ID: `<ENTER_IN_PORTAL_ONLY>`
- Social/profile URLs: `<PUBLIC_SOCIAL_URLS>`

## Social post

I built Underwriter for the Alpaca AI Trading Agents Hackathon: a paper-only agent that
sells defined-risk volatility insurance on liquid ETFs and declines far more often than
it writes. The model can only veto; deterministic code owns capital, execution, exits,
and recovery. Every refusal is evidence. `<PUBLIC_SUBMISSION_URL>`
