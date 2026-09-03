# Underwriter submission package

Start here. This directory contains every non-secret artifact that can be prepared
before the judged account, public URLs, live results, and recording exist.

## Judge story

Underwriter sells **defined-risk volatility insurance on liquid ETFs** and declines far
more often than it writes. The product idea is simple: compare implied volatility with
what the underlying has actually realised, then collect premium only where the spread
is rich enough. The engineering claim is stricter: deterministic code owns every
capital-affecting decision, the model can only veto, unknown inputs deny, and every
decision is journalled for replay.

## Artifact index

| Artifact | Purpose |
|---|---|
| [`one-page.md`](one-page.md) | Complete AI logic, risk-gate, infrastructure, and limitations write-up |
| [`integration-probes.md`](integration-probes.md) | Live Alpaca order/cancel evidence plus the composed Railway restart proof |
| [`demo-script.md`](demo-script.md) | Credential-safe, sub-five-minute recording script |
| [`underwriter-submission.pptx`](underwriter-submission.pptx) | Editable presentation source |
| [`underwriter-submission.pdf`](underwriter-submission.pdf) | Submission-ready slide deck PDF |
| [`assets/underwriter-cover.png`](assets/underwriter-cover.png) | 16:9 cover image |
| [`performance-report-template.md`](performance-report-template.md) | Honest judged-run P&L report template |
| [`submission-copy.md`](submission-copy.md) | Copy-ready title, descriptions, tags, and social copy |
| [`operator-checklist.md`](operator-checklist.md) | Only the remaining actions that cannot be done from the repository |

## Verified repository evidence

```text
pytest: 1,107 passed
ruff:   all checks passed
mypy:  success, 48 source files checked
```

The restart requirement is closed by two explicit halves:

1. A forced Railway redeploy remounted the same persistent volume and rebuilt the agent.
2. A four-process deterministic regression recovered live and exploratory open state,
   reconciled an accepted close without resubmitting it, drained each consequence once,
   and replayed the final terminal state without duplication.

## What is deliberately not in this package

No API keys, Railway variables, competition account ID, private dashboard data, or
credential-bearing screenshots belong in git. The final URLs and account ID are entered
directly in the submission portal after the operator completes the linked checklist.
