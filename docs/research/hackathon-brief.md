# Hackathon brief

Research date: August 26, 2026. Revalidated against the live event page on
August 28, 2026 at kickoff; corrections are marked **[revalidated 08-28]**.  
Official event: [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)

## Schedule

- Kickoff: August 28, 2026 at 10:00 AM CDT.
- Submission deadline: September 4, 2026 at 10:00 AM CDT.
- Because of the kickoff and deadline times, the practical live-market evaluation window is only about four and a half US trading sessions.

## Main challenge

Build an autonomous AI trading agent that attempts to generate P&L on Alpaca. The project must present a clear, testable strategy and demonstrate how the agent:

1. Finds opportunities.
2. Makes trading decisions.
3. Applies risk gates.
4. Opens, monitors, and closes positions.
5. Performs during the competition.

## Non-negotiable requirements

- Use Alpaca's Trading API.
- Build an autonomous agent.
- Use either Alpaca's MCP server or Alpaca CLI.
- Incorporate options trading.
- Develop and trade in Alpaca's paper environment; no real capital is required or authorized for this project.
- Teams may have one to six members.
- The repository must be public for submission.
- **[revalidated 08-28]** The prize terms state "Submissions must be original and
  MIT-compliant", so the repository carries an MIT `LICENSE`.
- **[revalidated 08-28]** The event now advertises a single track, "Main Challenge —
  Options Alpha Agents". Earlier volatility/hedging/portfolio-overlay tracks appear
  to have been consolidated.
- **[revalidated 08-28]** Registration on both lablab.ai and the lablab Discord is
  required to participate. Team creation is gated on a connected Discord account.
  No registration cutoff is published.

## Account rules

- Any paper account may be used during development.
- The judged submission must run on a brand-new paper account dedicated to this hackathon.
- That competition account must start at exactly $100,000.
- A reused account is ineligible for judging.
- Preserve the competition account ID for the submission, but do not commit it with credentials.

## Judging and prizes

The published judging areas are:

- P&L performance.
- Technology implementation.
- Creativity and originality.
- Presentation and execution.
- A social/build-in-public component is also promoted.

**[revalidated 08-28]** The published prize pool is now **$6,300**, not $6,000:

- 1st: $2,500 **plus $300 in Featherless credits**.
- 2nd: $1,500. 3rd: $1,000.
- Two social prizes of $500 per team.
- Alpaca pays the $6,000 cash pool directly in USD; the extra $300 is partner credit,
  and partner prizes require the partner technology to be integrated.

**[revalidated 08-28] Algo Trader Plus is narrower than first recorded.** It is not one
month per participant across the board. It goes only to members of the two
social-prize-winning teams. **Plan for Basic-plan market data permanently:** no OPRA
quotes, IEX equities, 15-minute-delayed option trades, indicative option quotes.

Prizes are paid to **individuals, not teams**. One member is designated for the full
amount unless a split is agreed with Finance in advance. Winners must be 18+ and supply
W-9/W-8BEN, photo ID, and bank details within 90 days of notification or forfeit.
Non-US winners face 30% US withholding absent a treaty claim.

## Submission package

Prepare all of the following before the deadline:

- Project title, short description, long description, and tags.
- 16:9 cover image.
- Demo video: under five minutes **and under 300MB**. 16:9 is *recommended*, not
  required — both constraints are lablab-generic rather than stated on the event page.
- Slide presentation. **[revalidated 08-28]** PDF specifically is not a stated
  requirement; it appears only in the Rule Book's scoring rubric. Use PDF as the safe
  default.
- Public GitHub repository.
- Hosted application URL.
- Dedicated paper account ID.
- One-page write-up explaining the AI logic, risk gates, and Alpaca infrastructure.
- Up to five social links, if used.

## Implications for the build

- Optimize for a narrow, observable strategy instead of a general-purpose trading chatbot.
- Make every rejection and decision explainable in the dashboard and audit log.
- Keep setup reliable enough to survive the compressed live window.
- Separate official paper-account P&L from a conservative shadow P&L that models spread and slippage.
- Treat the demo, one-page write-up, and performance evidence as first-class deliverables.



## Open question: the judged performance window

**[revalidated 08-28]** The event page does not define the P&L measurement window. The
criterion reads only: "The trading performance of the submitted agent in the Alpaca
paper trading environment. Judges will consider the project's P&L and how effectively
the strategy performs through its trading activity." The challenge text says
performance is judged "over the course of the competition", implying August 28 to
September 4, but no start or end timestamp is published, and there is no rule about
when the fresh competition account must be opened or funded relative to that window.

This decides whether the competition account is created Monday morning or later in the
week. **Ask in Discord before creating it.**

## Submission grace period

The lablab Rule Book allows manual submission for six hours after the deadline "for
those with valid reasons and prior approval from organizers or mentors". This is
discretionary, not a right. Do not plan around it.
