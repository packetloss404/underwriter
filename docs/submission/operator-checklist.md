# Final operator checklist

Everything below requires account access, secrets, a live market window, recording, or
the submission portal. There are no remaining repository-only packaging steps.

## 1. Publish and verify the repository

- Change `packetloss404/underwriter` from private to public.
- Open the repository in a signed-out/incognito browser and verify the README, license,
  deck PDF, cover image, and this submission index are reachable.
- Paste the public repository URL into the submission portal.

## 2. Finish the judged Railway/account wiring

- Create or select the brand-new Alpaca competition paper account with exactly
  **$100,000**. Never commit its credentials or account ID.
- Set the Alpaca credentials and one supported model-provider key as Railway variables.
- Confirm the persistent volume is mounted at `/data`, `UNDERWRITER_DRY_RUN=true`,
  `UNDERWRITER_KILL_SWITCH=false`, one replica, restart policy `ALWAYS`, and Serverless
  remains off.
- Generate or confirm the public Railway domain. Load it signed out and verify the
  dashboard exposes no mutation route, credential, account ID, or private header.
- During market hours, watch one complete dry-run cycle. Only then set
  `UNDERWRITER_DRY_RUN=false` if every preflight and quote-freshness gate is healthy.

## 3. Capture the judged-run evidence

- Record the official and shadow P&L, equity start/end, positions, fills, refusals, and
  data limitations in [`performance-report-template.md`](performance-report-template.md).
- If a real fill exists, record the parent-versus-leg reporting units and run the CLI
  `get-by-client-id` reconciliation probe. Do not manufacture a fill for evidence.
- Export or capture only secret-free dashboard evidence. Keep account IDs and variables
  out of screenshots.

## 4. Record and upload the demo

- Follow [`demo-script.md`](demo-script.md); keep the result at five minutes or less and
  under the event's upload limit.
- Say "paper trading" at least twice and identify whether the shown cycle is dry-run or
  submission-enabled.
- Review every frame for credentials or account identifiers before uploading.

## 5. Submit before the deadline

- In the portal, enter the competition paper account ID, public app URL, public repo URL,
  video URL, descriptions, tags, and social links from [`submission-copy.md`](submission-copy.md).
- Upload the deck PDF and cover image from this directory.
- Re-open every submitted URL signed out, then submit before **September 4, 2026 at
  10:00 AM CDT**.
