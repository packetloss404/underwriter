# Underwriter judged-run performance report

> Populate this file from the final judged account and dashboard. Do not commit the
> competition account ID, API keys, Railway variables, or credential-bearing screenshots.

## Run identification

| Field | Value |
|---|---|
| Judged window | `<START_UTC>` to `<END_UTC>` |
| Repository commit | `<PUBLIC_MAIN_SHA>` |
| Public dashboard | `<PUBLIC_DASHBOARD_URL>` |
| Starting equity | `$100,000.00` |
| Ending equity | `<ENDING_EQUITY>` |

## Performance

| Measure | Result | Interpretation |
|---|---:|---|
| Official paper P&L | `<OFFICIAL_PNL>` | Alpaca paper-account result |
| Conservative shadow P&L | `<SHADOW_PNL>` | Worse of actual fill and submitted limit, less $0.05 per spread |
| Return on starting equity | `<RETURN_PCT>` | Official P&L / $100,000 |
| Maximum aggregate open risk | `<MAX_OPEN_RISK>` | Compare with the 3% portfolio cap |
| Positions opened / closed | `<OPENED>` / `<CLOSED>` | Defined-risk spreads only |
| Decisions / refusals | `<DECISIONS>` / `<REFUSALS>` | The refusal ledger is part of the result |

## Reconciliation

| Check | Result |
|---|---|
| Every submitted client order ID accounted for | `<YES/NO + NOTE>` |
| Parent and leg fills reconciled separately | `<YES/NO/NO FILLS>` |
| Unknown or ambiguous order outcomes left unresolved | `<COUNT + NOTE>` |
| Restart or redeploy during judged window | `<NONE or EVENT + RECOVERY RESULT>` |

## What the numbers do not prove

- Four sessions cannot establish the long-run loss distribution of a short-volatility
  strategy; a green window does not validate it and a red window does not refute it.
- Free-tier options data is indicative rather than OPRA NBBO, option trades are delayed,
  and Alpaca's paper multi-leg fill model is undocumented.
- Official paper P&L is reported because it is the judged metric. Shadow P&L is the more
  conservative execution estimate and should be read beside it.
- No live-money performance is claimed. Underwriter is paper-only by construction.

## Reproducible export

```bash
uv run underwriter-export --journal /data/underwriter.db --out artifacts/dashboard
```

The generated dashboard snapshot keeps `data_age_seconds` unset so the browser computes
freshness from `generated_at` instead of presenting a frozen snapshot as current.
