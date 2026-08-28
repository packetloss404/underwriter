# Catalyst Convexity

Research repository for the 2026 Alpaca AI Trading Agents Hackathon.

## Status

Research was captured on August 26, 2026. Implementation is intentionally paused until the official kickoff on **August 28, 2026 at 10:00 AM America/Chicago**.

The working concept is **Catalyst Convexity**: an autonomous, paper-only options agent that trades defined-risk vertical spreads when a news catalyst, underlying price action, volatility, and contract liquidity agree.

## Research dossier

- [Hackathon brief](docs/research/hackathon-brief.md)
- [Alpaca platform notes](docs/research/alpaca-platform.md)
- [Strategy specification](docs/research/strategy-spec.md)
- [Kickoff checklist](docs/research/kickoff-checklist.md)
- [Source index](docs/research/sources.md)

## Guardrails

- Paper trading only.
- Options are mandatory; initial execution is limited to defined-risk multi-leg positions.
- AI interprets unstructured information and produces a structured thesis. Deterministic code owns pricing, sizing, order construction, risk gates, and the kill switch.
- The final judged run must use a brand-new Alpaca paper account with exactly $100,000 starting equity.
- No API keys, account IDs, or other secrets belong in this repository.

