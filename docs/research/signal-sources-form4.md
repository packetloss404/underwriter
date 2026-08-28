# Signal source assessment: SEC EDGAR Form 4 (insider transactions)

> **Superseded, 2026-08-28.** This report was commissioned while the strategy was a
> disclosure-driven directional tilt. That approach was abandoned the same day: the
> density arithmetic in this document killed the congressional variant, and switching
> to SEC Form 4 fixed filing density but not the underlying problem, since the
> documented insider-buy effect is a multi-month drift and the judged window is four
> sessions. The active strategy harvests the volatility risk premium instead and uses
> no disclosure feed at all. See `strategy-spec.md`.
>
> The research is retained because it is sound, it records why the idea was rejected,
> and the H.R. 7008 advance-notice provision would make the approach viable in future.

**Project:** Rotunda
**Author:** research agent
**Date:** 2026-08-28
**Status:** Complete. Verdict is negative for the Rotunda use case — see [Verdict for sector aggregation](#verdict-for-sector-aggregation).

Every EDGAR endpoint, URL pattern and XML field in Part A was verified live against
sec.gov on 2026-08-28. Every volume figure in Part B was computed from the SEC's own
quarterly DERA dataset, not taken from a vendor. Where something could not be verified it
is marked **UNKNOWN**.

---

## Contents

- [A. Access and format](#a-access-and-format)
- [B. Signal quality](#b-signal-quality)
- [C. Practicality](#c-practicality)
- [Verdict for sector aggregation](#verdict-for-sector-aggregation)
- [Appendix: reproduction commands](#appendix-reproduction-commands)

---

## A. Access and format

### A1. Discovery endpoints

#### (i) Current-events Atom feed — the near-real-time one

```
https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=100&output=atom
```

**Gotcha 1 — `owner=only` is mandatory.** `type=4` on its own is a *prefix* match, not an
exact match. Without `owner=only` the feed returns Form 4 mixed with `424B2`, `485BPOS`,
`497` and `497K`. Measured on 2026-08-28:

| Query | Form 4 | Form 4/A | Junk |
|---|---|---|---|
| `type=4&owner=include&count=40` | 18 | 2 | 20 (424B2, 485BPOS, 497, 497K) |
| `type=4&owner=only&count=100` | 96 | 4 | 0 |

**Gotcha 2 — one `<entry>` per filer CIK, not per filing.** The issuer and every reporting
owner each get an entry, all sharing one accession number. A 100-entry pull contained only
**50 distinct accessions**. Dedupe on the accession in `<id>`:

```
<id>urn:tag:sec.gov,2008:accession-number=0001683168-26-006783</id>
```

`count` caps at 100. Entry shape:

```xml
<entry>
  <title>4 - Bunting Eric (0001702927) (Reporting)</title>
  <link rel="alternate" type="text/html"
        href="https://www.sec.gov/Archives/edgar/data/1702927/000168316826006783/0001683168-26-006783-index.htm"/>
  <summary type="html"><b>Filed:</b> 2026-08-28 <b>AccNo:</b> 0001683168-26-006783 <b>Size:</b> 11 KB</summary>
  <updated>2026-08-28T16:31:55-04:00</updated>
  <category scheme="https://www.sec.gov/" label="form type" term="4"/>
  <id>urn:tag:sec.gov,2008:accession-number=0001683168-26-006783</id>
</entry>
```

`<updated>` is the EDGAR **acceptance timestamp**. `term="4"` on the `<category>` element is
the reliable form-type discriminator — filter on it rather than trusting the query string.

#### (ii) Daily index files — end-of-day reconciliation only

```
https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260827.idx
https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/company.20260827.idx
https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/master.20260827.idx
https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/index.json      # directory listing
```

Fixed-width text, sorted by form type, with a header block to skip:

```
Form Type   Company Name                                CIK       Date Filed  File Name
4           ABBOTT LABORATORIES                         1800      20260827    edgar/data/1800/0001612571-26-000011.txt
```

These are **built around 22:00 ET**, so they are useless intraday. Same one-line-per-filer
duplication as the feed: 2026-08-27 had **870 raw Form 4 lines → 425 unique accessions**
(plus 17 `4/A`). Raw line counts over-report by roughly 2.08x.

Note from SEC's own documentation: full and quarterly indexes are **rebuilt weekly on
Saturday mornings** to absorb post-acceptance corrections, so a filing you saw on Tuesday
can legitimately vanish from a later index.

#### (iii) Full-text search API

```
https://efts.sec.gov/LATEST/search-index?q=&forms=4&startdt=2026-08-27&enddt=2026-08-27
```

`q` may be empty. Returns raw Elasticsearch JSON:

```json
{"took":446,"hits":{"total":{"value":442,"relation":"eq"},"hits":[
  {"_index":"edgar_file","_id":"0002151913-26-000005:wk-form4_1787861494.xml",
   "_source":{"ciks":["0002151913","0000706129"],"period_ending":"2026-08-26",
              "display_names":["Ritter Nicholas  (CIK 0002151913)","HORIZON BANCORP INC /IN/  (CIK 0000706129)"],
              "root_forms":["4"],"file_date":"2026-08-27","form":"4","sics":["6022"],...}}]}
```

**Cross-validation:** FTS reported `hits.total.value = 442` for 2026-08-27. The daily index
gave 425 Form 4 + 17 Form 4/A = **442 exactly**. Two independent endpoints agree, so both
are complete. `forms=4` includes `4/A`.

Useful extra: `_source.sics` carries the issuer SIC code, which saves a lookup. Paginate
with `&from=N` (100/page). FTS indexing **lags the Atom feed** — use the feed for live
discovery and FTS for historical work.

#### "All Form 4 filings filed today"

Poll the Atom feed intraday, dedupe by accession, then reconcile against
`form.YYYYMMDD.idx` after 22:00 ET to catch anything the feed dropped.

### A2. Form 4 XML structure

Yes — fully structured XML, root element `<ownershipDocument>`, schema version `X0609`.

**The document filename is not predictable.** Observed in the wild:
`form4-08272026_090838.xml`, `wk-form4_1787862464.xml`, `primary_doc.xml`. You must read the
filing's `index.json` first, so budget **2 requests per filing**:

```
https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION_NO_DASHES}/index.json
https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION_NO_DASHES}/{filename}.xml
```

Worked example (real code-P filing, IPG Photonics, verified 2026-08-28):

```
https://www.sec.gov/Archives/edgar/data/1053572/000111192826000163/wk-form4_1787862464.xml
```

#### Field map

| Field | XPath |
|---|---|
| Document type | `/ownershipDocument/documentType` (`4`) |
| Period of report | `/ownershipDocument/periodOfReport` |
| Issuer CIK | `/ownershipDocument/issuer/issuerCik` (zero-padded to 10) |
| **Issuer ticker** | `/ownershipDocument/issuer/issuerTradingSymbol` |
| Issuer name | `/ownershipDocument/issuer/issuerName` |
| Owner CIK | `/ownershipDocument/reportingOwner/reportingOwnerId/rptOwnerCik` |
| Owner name | `/ownershipDocument/reportingOwner/reportingOwnerId/rptOwnerName` |
| Director flag | `…/reportingOwnerRelationship/isDirector` |
| Officer flag | `…/reportingOwnerRelationship/isOfficer` |
| 10% owner flag | `…/reportingOwnerRelationship/isTenPercentOwner` |
| Other flag | `…/reportingOwnerRelationship/isOther` |
| **Officer title** | `…/reportingOwnerRelationship/officerTitle` |
| **10b5-1 plan flag** | `/ownershipDocument/aff10b5One` (document-level, `0`/`1`) |
| Transaction date | `…/nonDerivativeTransaction/transactionDate/value` |
| **Transaction code** | `…/nonDerivativeTransaction/transactionCoding/transactionCode` |
| Form type on coding | `…/transactionCoding/transactionFormType` |
| Equity swap flag | `…/transactionCoding/equitySwapInvolved` |
| Shares | `…/transactionAmounts/transactionShares/value` |
| Price per share | `…/transactionAmounts/transactionPricePerShare/value` |
| Acquired/disposed | `…/transactionAmounts/transactionAcquiredDisposedCode/value` (`A`/`D`) |
| Shares owned after | `…/postTransactionAmounts/sharesOwnedFollowingTransaction/value` |
| Direct/indirect | `…/ownershipNature/directOrIndirectOwnership/value` (`D`/`I`) |
| Nature of indirect | `…/ownershipNature/natureOfOwnership/value` |
| Security title | `…/securityTitle/value` |
| Footnote text | `/ownershipDocument/footnotes/footnote[@id]` |

Derivative table adds `conversionOrExercisePrice`, `exerciseDate`, `expirationDate`,
`underlyingSecurity/underlyingSecurityTitle`, `underlyingSecurityShares`.

Real code-P block, verbatim:

```xml
<nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    <transactionDate><value>2026-08-26</value></transactionDate>
    <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
    </transactionCoding>
    <transactionAmounts>
        <transactionShares><value>1552</value></transactionShares>
        <transactionPricePerShare><value>72.84</value><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
    <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>19728</value></sharesOwnedFollowingTransaction>
    </postTransactionAmounts>
    <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
    </ownershipNature>
</nonDerivativeTransaction>
```

#### Four parser traps, all confirmed by inspection

1. **Booleans are inconsistently serialized.** Abbott's filing used
   `<isDirector>true</isDirector>`; IPG's used `<isDirector>1</isDirector>`. Accept
   `true`/`false`/`1`/`0`.
2. **Footnotes carry load-bearing data.** Almost any value can have a sibling
   `<footnoteId id="Fn"/>`, and **prices are frequently weighted averages**. Abbott reported
   a sale at `115.57` whose footnote reads: *"The price reported in Column 4 is a weighted
   average price. These shares were sold in multiple transactions at prices ranging from
   $115.09 to $116.08."* A parser that ignores footnotes silently records a fictional price.
3. **`periodOfReport` ≠ `transactionDate`,** and one filing holds many transactions across
   both tables. Abbott's single filing contained 2×M, 2×S, 1 holding and 2 derivative M rows.
   Never collapse a filing to one transaction.
4. **Multi-owner filings** (>16% of Form 4s) have repeated `<reportingOwner>` blocks under
   one accession. The standard academic vendor datasets collapse these to the first filer and
   drop ~6.5% of observations; the raw XML and the DERA TSVs do not.

### A3. SEC fair-access policy — verbatim

From <https://www.sec.gov/os/webmaster-faq> and
<https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>:

> "We allow scripted access to sec.gov content"

> "Note that our current maximum access rate is **10 requests per second**. This is carefully
> monitored to preserve equitable access for all users."

> "The SEC does not allow botnets or automated tools to crawl the site. Any request that has
> been identified as part of a botnet or an automated tool outside of the acceptable policy
> will be managed to ensure fair access for all users."

> "To ensure everyone has equitable access to SEC EDGAR content, please use efficient
> scripting. Download only what you need and please moderate requests to minimize server
> load."

> "Please declare your user agent in request headers:
> **Sample Declared Bot Request Headers:**
> `User-Agent: Sample Company Name AdminContact@<sample company domain>.com`
> `Accept-Encoding: gzip, deflate`
> `Host: www.sec.gov`"

**Summary:** programmatic polling is explicitly permitted. Limit is **10 req/s**. The
User-Agent must be *company/app name + contact email*, not a bare email address. An
undeclared UA produces an "Undeclared Automated Tool" error. Note `data.sec.gov` requires
`Host: data.sec.gov` rather than `www.sec.gov`.

### A4. Latency — measured, and it is genuinely good

SEC's stated figure:

> "Filings are often available on sec.gov within **1-3 minutes** of the EDGAR system
> timestamp. The lag time can increase significantly with high server load. We don't
> guarantee and cannot predict this lag."

Measured better than stated. At wall-clock `17:02:22 ET` the newest Atom entry carried
acceptance timestamp `17:02:24 ET` — **sub-minute, effectively real-time**.

Two operational caveats:

- The full-text search index lags the Atom feed. Use the feed for live discovery.
- **EDGAR accepts filings until 22:00 ET.** A large share of Form 4s land *after the close*,
  so the tradeable reaction is next-morning, not same-session. This matters for any strategy
  that wants to act on the filing intraday.

---

## B. Signal quality

### B5. Transaction codes

| Code | Meaning | Carries information? |
|---|---|---|
| **P** | Open-market or private **purchase** | **Yes — the operative code** |
| **S** | Open-market or private sale | Weakly; heavily contaminated by diversification, 10b5-1 and post-exercise selling |
| A | Grant, award or other acquisition under Rule 16b-3(d) | **No** — compensation |
| M | Exercise/conversion of a derivative security | **No** — almost always paired with an immediate S |
| F | Shares withheld by issuer to satisfy tax withholding | **No** — and it is coded as a *disposition*, so a naive "insider sold" parser generates pure noise |
| G | Bona fide gift | No |
| C | Conversion of a derivative security | No |
| D | Disposition to the issuer | No |
| J | Other (footnote-defined) | Unclassifiable without reading the footnote |
| X, U, L, I, W, O | Option expiration, tender, small-acquisition, discretionary, will/laws of descent, other | No |

Only **P** is unambiguously an information-bearing, discretionary, cash-out-of-pocket
decision. Everything in the A/M/F/D family is compensation mechanics.

### B6. Volume — computed from SEC DERA, then independently cross-validated

The SEC publishes every Form 3/4/5 as quarterly TSVs:

<https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets>

**URL gotcha:** the path is not stable across quarters. Historical quarters live at
`/files/structureddata/data/insider-transactions-data-sets/{YYYY}q{N}_form345.zip`, but
2026Q2 has moved to
`/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip`.
Cross-combinations 404. **Scrape the landing page for `href` matching `form345.zip`** rather
than hardcoding. Coverage runs 2006Q1 → 2026Q2.

Files inside `2026q2_form345.zip` (11.5 MB), joined on `ACCESSION_NUMBER`:

| File | Rows | Key columns |
|---|---|---|
| `SUBMISSION.tsv` | 56,102 | `FILING_DATE`, `PERIOD_OF_REPORT`, `DOCUMENT_TYPE`, `ISSUERCIK`, `ISSUERNAME`, `ISSUERTRADINGSYMBOL`, `AFF10B5ONE` |
| `NONDERIV_TRANS.tsv` | 78,328 | `TRANS_CODE`, `TRANS_DATE`, `TRANS_SHARES`, `TRANS_PRICEPERSHARE`, `TRANS_ACQUIRED_DISP_CD`, `SHRS_OWND_FOLWNG_TRANS`, `DIRECT_INDIRECT_OWNERSHIP` |
| `DERIV_TRANS.tsv` | 30,370 | as above plus `CONV_EXERCISE_PRICE`, `EXPIRATION_DATE`, `UNDLYNG_SEC_*` |
| `REPORTINGOWNER.tsv` | 60,153 | `RPTOWNERCIK`, `RPTOWNERNAME`, `RPTOWNER_RELATIONSHIP`, `RPTOWNER_TITLE` |
| `NONDERIV_HOLDING.tsv` / `DERIV_HOLDING.tsv` | 22,746 / 12,021 | position snapshots |
| `FOOTNOTES.tsv` | 129,689 | `FOOTNOTE_ID`, `FOOTNOTE_TXT` |
| `OWNER_SIGNATURE.tsv` | 59,580 | signature name and date |

Nearly every transaction field has a parallel `_FN` footnote-id column — same trap as the raw
XML.

#### Filings per trading day

- **SEC DERA official counts** (<https://www.sec.gov/data-research/sec-markets-data/number-edgar-filings-form-type>):
  CY2025 = **169,532 Form 4** + 2,555 Form 4/A; 2024 = 177,980; all-time peak 234,776 (2007).
  Form 4 is the **single most-filed form type on EDGAR**, roughly 20% of all filings.
- **Computed from 2026Q2:** 50,545 Form 4/4A over 63 filing days = **mean 802/day**, median
  786, min 293, max 2,151.
- **Counted from daily indexes, Aug 2026:** 425–668/day (Aug 19: 668, Aug 27: 425).

**Seasonality dominates the mean.** CY2025 by month: Feb ~1,130/day and Mar ~1,025/day
(grant and vesting season) versus Oct ~425/day. Single-day max 3,496 (2025-03-04). Size for
~1,200 at p90 and a 3,500 spike — but note **late August is the quiet end of the range**.

#### Transaction code distribution — computed, 2026Q2, Form 4/4A, `NONDERIV_TRANS.tsv`, n = 78,181

| Code | Meaning | Count | Share |
|---|---|---|---|
| S | Sale | 27,598 | **35.30%** |
| A | Grant/award | 19,209 | **24.57%** |
| M | Option exercise | 10,778 | **13.79%** |
| F | Tax withholding | 9,357 | **11.97%** |
| **P** | **Open-market purchase** | **5,311** | **6.79%** |
| J | Other | 1,746 | 2.23% |
| D | Disposition to issuer | 1,710 | 2.19% |
| G | Gift | 1,282 | 1.64% |
| C | Conversion | 880 | 1.13% |
| X/U/L/I/W/O | Remainder | 308 | 0.39% |

`DERIV_TRANS.tsv` (n = 30,357): A 44.18%, M 38.12%, D 5.90%, C 3.57%, S 2.84%, J 2.46%,
**P 1.04%**, G 0.91%.

Per-filing view across all 50,545 Form 4/4A: at least one A in 53.96%, S in 21.24%, M in
15.72%, F in 14.41%, **P in 6.86%**, D in 2.84%, G in 1.86%.

**Compensation mechanics (A + M + F + D) are ~60% of all transaction lines. Only ~7% of Form
4 filings contain a genuine open-market buy.**

Corroboration over a longer window: *Scientific Data* 10:237 (2023), Table 4 — 10,323,646
transaction lines parsed from raw EDGAR XML, 2003–2022 — puts non-derivative P at **12.11%**
(S 40.07%, A 15.07%, M 13.11%, F 8.91%, G 2.73%).
<https://www.nature.com/articles/s41597-023-02147-6>

**The P share has roughly halved over two decades** (12.1% → 6.8%) as equity compensation
grew. The current regime is the lower number. A "12.7%" figure circulating online traces only
to an unsourced Medium post — do not cite it.

#### The funnel, per trading day (2026Q2, 63 filing days)

| Metric | Mean | Median | Min | Max |
|---|---|---|---|---|
| Form 4/4A filings | 802.3 | 786 | 293 | 2,151 |
| Distinct issuers, any Form 4 | 295.8 | 296 | 145 | 590 |
| Filings containing a code-P | 53.1 | 51 | 18 | 104 |
| **Distinct issuers with ≥1 code-P** | **40.7** | 40 | 13 | 76 |
| …where the P filer is Officer or Director | 32.6 | 30 | 10 | 67 |
| …with aggregate P notional > $25k | 28.7 | 27 | — | — |
| …with aggregate P notional > $100k | 18.8 | 18 | — | — |
| …with aggregate P notional > $1M | 5.9 | 6 | — | — |
| **…that are S&P 500 members** | **2.9** | **2** | 0 | 9 |

Over the whole quarter, 1,188 of 4,508 distinct issuers (26.4%) had at least one code-P buy,
but **only 79 of 500 S&P 500 members (15.7%) did.**

**Trade size,** code-P non-derivative, 2026Q2, n = 5,215: median **$41,474**, p25 $6,979,
p75 $200,577, p90 $1,033,000, p99 $25M. 71.1% ≥ $10k, 35.1% ≥ $100k, **only 10.5% ≥ $1M**.

#### Independent cross-validation

Scraping OpenInsider's trailing 30-day window (2026-07-30 → 2026-08-28, 1,270 rows, 571
distinct tickers) gave **mean 42.0 distinct buy tickers/day** and **28.6 / 18.6 / 5.1** at the
$25k / $100k / $1M thresholds — matching the DERA computation to within ~1 issuer/day at
every level. Two fully independent methods, same answer.

Practical notes on OpenInsider if used as a cross-check: `fd=0` means **"All dates," not
today** (use `fd=1`); pages hard-cap at 100 rows unless you pass `cnt=5000`; the
`/latest-cluster-buys` table uses a **different schema** (an `Ins` count column instead of
Insider Name/Title), so a parser must branch on the header row.

**Data quality warning:** the feed contains genuine garbage. `SCTH Securetech Innovations`
reported `$100,000.00/share × 1,000 shares = $100,000,000`. Price-sanity filtering is
mandatory, not optional.

Cluster buys (multiple insiders, same issuer, same window) are rare: **~73 in 30 days ≈ 1.3
per trading day**, median $1.04M.

### B7. Academic literature on holding period — the make-or-break question

Short answer: **both a short-window filing reaction and a multi-month drift exist, but only
the drift is robust, and neither is harvestable in four sessions net of costs.**

#### The decomposition study

**Jeng, Metrick & Zeckhauser (2003), *Review of Economics and Statistics* 85(2), 453–471.**
The only major paper that decomposes abnormal return by event-time bucket. Purchases,
value-weighted monthly alphas, anchored on the **transaction** date, 1975–1996:

| Window | CAPM α/mo | 4-factor α/mo | Char-adjusted α/mo |
|---|---|---|---|
| day0–day5 | 2.69% (se 0.32)** | 2.52% (0.32)** | 3.04% (0.31)** |
| day5–day21 | 1.29% (0.26)** | 1.14% (0.26)** | 1.36% (0.25)** |
| day21–month6 | 0.54% (0.20)** | **0.29% (0.16) n.s.** | 0.36% (0.18)* |

Headline: 50–67 bp/month abnormal, ~10.2%/yr raw outperformance. *"About one-quarter of these
abnormal returns accrue within the first five days after the initial transaction, and one-half
accrue within the first month."*

**Three caveats that gut the short-horizon case:**

1. **Anchored on the transaction date, not the filing date.** In their pre-SOX sample the
   trade was not public for weeks — *"For most insider transactions, more than 21 days pass
   before the transactions get reported and made public."* The day0–day5 alpha is **the
   insider's return, not an outsider's.** The window an outsider could actually access is
   day21–month6, where the 4-factor alpha is **insignificant**.
2. They convert and then kill it themselves: *"Assuming a one percent roundtrip transaction
   cost, the day0-day5 portfolio would incur approximately 400 basis points in transactions
   costs per month. Thus, the abnormal returns are not sufficient to allow a profitable
   trading strategy after transactions costs, even if such a trading strategy were otherwise
   feasible."*
3. They find **no significant size effect** (small firms do not beat large) and **no
   significant role effect** (top executives do not beat other insiders) — directly
   contradicting Lakonishok–Lee and the CFO literature. This disagreement is real and
   unresolved.

#### The filing-date short window does exist

**Brochet (2010), *The Accounting Review* 85(2), 419–446.** Event window = three days
beginning with SEC receipt of the Form 4.

| | Pre-SOX | Post-SOX (2-business-day rule) |
|---|---|---|
| Purchases, mean CAR[0,+2] | +0.59% | **+1.89%** |
| Sales, mean CAR[0,+2] | −0.28% | −0.11% |
| Purchases, abnormal volume | +1.03% | **+12.03%** |

This is the high-water mark in the literature. Sample is 2003–2006; purchases in that era
were disproportionately small-cap and distressed; it is a raw event-study mean, not a
net-of-cost strategy return. *(The journal version is paywalled; these figures come from a
summary at <https://corpgov.law.harvard.edu/2009/10/30/sox-and-insider-trades/>. A
third-party citation gives a five-day figure of 1.0% pre / 2.3% post-SOX which I could not
verify — treat the 3-day numbers as primary.)*

#### Modern replication exposes the skew problem

Filing-date event study of C-suite code-P purchases, Jan 2022 – Jun 2026, 7,405 issuer×day
signals, SPY-adjusted:

| Horizon | Mean CAR | **Median** | t | 95% CI |
|---|---|---|---|---|
| 1 session | +0.534% | **+0.109%** | 6.46 | [+0.368%, +0.700%] |
| 5 sessions | **+1.009%** | **+0.203%** | 5.05 | [+0.608%, +1.409%] |
| 21 sessions | +0.980% | −0.295% | 1.68 | [−0.191%, +2.151%] |
| 63 sessions | +1.103% | −0.648% | 1.19 | [−0.751%, +2.956%] |
| BHAR 63 sessions | +0.104% | −3.605% | 0.11 | includes 0 |

**Mean +1.0% at five sessions; median +0.2%.** The signal is a fat right tail, not a broad
drift. The author explicitly notes there is no transaction-cost, spread or execution model,
and that the median insider purchase is 5.08% of trailing daily dollar volume (37.5% exceed
10%). Not peer-reviewed, but it is the only modern filing-date-anchored dataset I found.
<https://blog.quantinsti.com/sec-form-4-insider-trading-python-event-study/>

#### The paper that tests exactly this strategy

**Oenschläger & Möllenhoff (2025), *Finance Research Letters* 72, 106514, "Insider filings as
trading signals — Does it pay to be fast?"** Post-SOX, intraday data. Buy at the **5-minute
VWAP after publication of the filing**, sell at the close after N trading days. Verbatim:

> "positive but lower abnormal percentage returns than in previous studies for short holding
> periods… **vanish and even become negative when limiting the tradable dollar amount for each
> trading signal to a reasonable size**… returns in our setup are negatively correlated with
> stock liquidity, almost negating a potentially profitable and scalable trading strategy
> **even before considering transaction costs**."

This is the closest thing in the literature to a fast Form 4 strategy, and its answer is no.
<https://www.sciencedirect.com/science/article/pii/S1544612324015435>

#### The multi-month drift is weaker than folklore

- **Lakonishok & Lee (2001), *RFS* 14(1), 79–111.** *"In general, very little market movement
  is observed when insiders trade and when they report their trades to the SEC."* The market
  underreacts at the reporting date and impounds over the following ~6 months. Firm level:
  high- vs low-insider-buying firms **+7.8% over 12 months** raw, **+4.8%** after size and
  book-to-market adjustment. All of it from purchases; sales have no predictive power.
- **Cohen, Malloy & Pomorski (2012), *Journal of Finance* 67(3), 1009–1043.** Portfolios
  formed at end of month *t*, held month *t*+1 — a one-month horizon with an already-stale
  signal. Long side (purchases) monthly alphas:

  | | Opportunistic buys | Routine buys |
  |---|---|---|
  | VW CAPM | 0.87%*** (t=2.88) | 0.45%* (1.73) |
  | VW Fama-French | 0.64%** (2.16) | 0.18% (0.75) |
  | VW 5-factor | **0.72%** (2.27)** | 0.09% (0.34) |
  | EW 5-factor | **1.58%*** (7.03)** | 0.87%*** (5.00) |

  **The critical number:** the *undifferentiated* value-weighted long-short across the whole
  insider universe is *"only 21 basis points per month and is statistically insignificant
  (t=0.83)."* Naive "an insider bought, so buy" is nothing. You need an opportunistic/routine
  classifier to get significance at all. The EW/VW gap (1.58% vs 0.72%) **is** the small-cap
  concentration.
- **Heckmann, Jacobs & Schwarz (2025), SSRN 4537187.** 3.7M insider trades, ~350,000 insiders,
  34 countries, 2000–2021. A ~10-signal composite delivers ≥1%/month alpha **"predominantly in
  equal-weighted portfolios… strongest among small stocks"** and *"predictability decays
  substantially at 6–12-month horizons."* So even the drift is more like 1–3 months than 12,
  and there is no meaningful value-weighted alpha.
- **Cziráki & Gider (2021), *Review of Finance* 25(5), 1547–1580.** The **median insider earns
  $464/year**; average abnormal profit per trade ~$4,000, median $141. Returns are *negatively*
  correlated with trade size. *"Insiders with the largest superior information do not turn this
  advantage into large economic rents."* If the insiders cannot scale it, neither can we.
- **Seyhun (1986), *JFE* 16(2), 189–212.** ~3% over a five-month holding period; Seyhun
  explicitly analysed outsiders imitating after the public reporting date and concluded they
  **cannot cover transaction costs**. That finding is 40 years old and has never been
  overturned at the naive-strategy level.

### B8. Where the effect concentrates

**Cluster buys** are the best-documented free conditioner:

- Alldredge & Blank (2019), *Journal of Financial Research* 42: purchases within 2 days of a
  peer insider's purchase earn **2.1% over the next month** vs **1.2%** for solitary purchases.
- Kang, Kim & Wang (working paper, 1986–2016): over **21 trading days**, cluster purchases
  **3.8%** vs non-cluster **2.0%** — roughly 2×.

Both are quoted at 21-day/one-month horizons, not 1–5 days. And clusters occur only ~1.3
times per trading day market-wide.

**Role** is genuinely contested. Wang, Shin & Francis (2012), *JFQA* 47(4), 743–762: CFOs earn
a 12-month excess return ~5pp higher than CEOs (secondary reports: CFO 7.41% vs CEO 2.41%).
But CMP find the most informed opportunistic insiders are **local, non-executive** insiders at
geographically concentrated, poorly governed firms, and JMZ find no role effect at all.
"Officers > directors > 10% owners" is folklore with weak formal support.

**Size/liquidity concentration is confirmed by nearly everyone:**

- Lakonishok & Lee: predictability driven by small firms (~7.4%/12mo for small-cap purchases).
- CMP: EW alpha 2.2× the VW alpha.
- Heckmann et al.: "strongest among small stocks."
- Oenschläger & Möllenhoff: returns *negatively correlated with liquidity*.
- **FTSE-350 test (2005–2015, deliberately excludes small caps):** post-trade purchase CAR
  over [t+1, t+20] is **−0.812%, insignificant**, against a 2.9% average roundtrip cost. Strip
  out small caps and the anomaly is gone. <https://pmc.ncbi.nlm.nih.gov/articles/PMC8886886/>
- Contrarian data point: **JMZ find no size effect** in their value-weighted design, and argue
  the small-cap story may be an equal-weighting artifact. Worth taking seriously.

**Purchase size relative to existing holdings:** the clean result is on the *sell* side (only
large sales that are also a large % of holdings predict negative returns). I found **no
comparably clean buy-side result** for purchase size scaled by prior stake. **UNKNOWN.**

**The finding aimed directly at an options strategy:**

> **Jeon & Sulaeman (2024), *Journal of Corporate Finance* 87, "Corporate insider purchases
> and the options market: Competition among informed investors."** Insider purchases in stocks
> with relatively **high options trading activity are followed by negligible abnormal
> returns.** The positive six-month abnormal returns occur in stocks with **less active
> options trading**. The options market screens out uninformed trades and accelerates price
> discovery.

The two strongest cross-sectional conditioners in this literature — small-cap concentration
and *low* options activity — both point away from any universe with liquid listed options.

---

## C. Practicality

### C9. Python libraries (status as of 2026-08-28)

| Library | PyPI | Latest release | Last commit | Stars | Parses Form 4 XML into fields? | Active 2026? |
|---|---|---|---|---|---|---|
| **edgartools** | `edgartools` | **5.53.0 — 2026-08-25** | 2026-08-26 | 2,631 | **Yes, fully** | **Yes, very** |
| sec-edgar-downloader | `sec-edgar-downloader` | 5.1.0 — 2026-02-02 | 2026-02-02 | 718 | No — download only | Low |
| secedgar | `secedgar` | 0.6.0 — 2025-05-09 | 2025-12-09 | 1,411 | No — download only | Barely |
| datamule | `datamule` | 5.0.2 — 2026-07-27 | 2026-08-14 | 556 | Generic doc→dict, not Form 4 typed | Yes |
| sec-edgar-toolkit | `sec-edgar-toolkit` | 0.2.0 — 2026-08-18 | 2026-08-18 | 38 | Yes (Forms 3/4/5) | Yes, immature |
| edgar-tool | `edgar-tool` | 2.1.2 — 2025-05-15 | 2025-05-15 | 211 | No — FTS CLI | Stalled |
| edgar-crawler | not on PyPI | — | 2025-07-18 | 542 | No — 10-K item text | Stale |
| sec-downloader | `sec-downloader` | 0.12.2 — 2025-05-20 | ~2025 | 60 | No — wraps downloader | Stalled |
| python-edgar | `python-edgar` | 3.1.3 — 2021-08-13 | 2023-05-05 | 355 | No — index files only | **Dead** |
| sec-parsers | `sec-parsers` | 0.549 — 2024-07-29 | repo gone | — | No | **Dead** |

**Recommendation: `edgartools`, pinned** (`edgartools==5.53.0`). <https://github.com/dgunning/edgartools>

It is the only mature library that models `ownershipDocument`. It exposes a `Form4` object
with `transactions`, `get_transaction_activities()`, `get_ownership_summary()`,
`to_dataframe()`, and convenience properties `market_trades`, `common_stock_purchases`,
`option_exercises`. Fields include transaction code, shares, `price_per_share`, value, A/D
flag, shares owned following, security title, insider name, officer/director/10% role, officer
title, direct vs indirect, and `has_10b5_1_plan`. **Critically it resolves footnote
references** — which matters given trap #2 above. MIT licensed, Python ≥3.10, ~935k
downloads/month.

Tradeoff: heavy, fast-moving dependency with a wide unused surface (XBRL, 13F, ADV). Release
cadence is aggressive (5.49.0 on Aug 15 → 5.53.0 on Aug 25), so minor-version churn is real.
Pin and test before bumping.

`sec-edgar-toolkit` is the only other free library that parses ownership forms, but it is
v0.2.0, 38 stars, and **AGPL-3.0** (commercial licence sold separately) — a licensing problem
for anything proprietary.

Purpose-built Form 4 repos are all hobby-scale — nothing above 7 stars, none on PyPI
(`bagoldbe/form4lab` 7★, `kenny-hk/sec-form4-api` 2★, `efebiskin/sec-form4-parser` 0★).

**`sec-api.io` is paid, confirmed.** Free tier is 100 total calls and *excludes* the Insider
Trading (Forms 3/4/5) API. Form 4 access starts at $49/mo annual.

**Suggested architecture:** use the **DERA quarterly TSVs for backtesting and calibration**
(already flat, complete, free — every volume number in this document came from them), and
`edgartools` only for the **live daily incremental**, since DERA lags a full quarter.

### C10. CIK → ticker mapping

Official files, both verified live (HTTP 200, 795 KB):

```
https://www.sec.gov/files/company_tickers.json
https://www.sec.gov/files/company_tickers_exchange.json     # adds exchange
```

Format:

```json
{"0":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"},
 "1":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}}
```

Note `cik_str` is an **integer**, not a zero-padded string, despite the name — pad to 10 to
join against EDGAR paths.

**You rarely need it.** `issuerTradingSymbol` is already inside the Form 4 XML and
`ISSUERTRADINGSYMBOL` is in DERA's `SUBMISSION.tsv`. Use the mapping file only as a fallback
and for exchange/listing status. Caveat: **92 of 1,181 code-P issuers last quarter were absent
from the SEC ticker file entirely** (mostly OTC and deregistered names).

For richer metadata including SIC code, use the per-company submissions API:

```
https://data.sec.gov/submissions/CIK0000001800.json
→ {"sic":"2834","sicDescription":"Pharmaceutical Preparations","tickers":["ABT"],"exchanges":["NYSE"]}
```

(~150 KB per company; requires `Host: data.sec.gov`.)

---

## Verdict for sector aggregation

Rotunda trades defined-risk verticals on 16 liquid ETFs and aggregates disclosure signals into
a per-sector conviction score. That reframing removes the single-name liquidity objection, so
it deserves its own answer. **The answer is still no, and for sharper reasons than at the
single-name level.**

### How many code-P buys per sector per week?

Computed directly from the DERA 2026Q2 dataset: **2,524 distinct (filing-day, issuer) code-P
events across 1,161 issuers over 63 filing days (12.6 weeks)**, after excluding derivative
transactions, Form 3/5, and price-insane rows. Issuer CIKs were mapped to SIC codes via
`data.sec.gov/submissions` (with `browse-edgar?output=atom` as fallback) and grouped to the
nearest Rotunda ETF. 71% of events resolved to a mapped sector; the remainder is shown
explicitly rather than silently redistributed.

| Sector (SIC-grouped) | Rotunda ETF | P-events/qtr | P-events/week | **≥$100k notional /week** |
|---|---|---:|---:|---:|
| Financials & Real Estate | **XLF** | 596 | 47.3 | **16.6** |
| Health Care | **XLV** | 358 | 28.4 | **14.5** |
| Technology | **XLK** | 207 | 16.4 | **7.0** |
| Industrials | **XLI** | 202 | 16.0 | **8.3** |
| Consumer Discretionary | **XLY** | 125 | 9.9 | **5.6** |
| Energy | **XLE** | 96 | 7.6 | **4.8** |
| Materials | **XLB** | 77 | 6.1 | **3.3** |
| Consumer Staples | **XLP** | 51 | 4.0 | **2.2** |
| Utilities | **XLU** | 46 | 3.7 | **1.7** |
| Semiconductors | **SMH** | 30 | 2.4 | **1.1** |
| Aerospace & Defense | **ITA** | 8 | 0.6 | **0.5** |
| _Unmapped SIC / lookup throttled_ | — | 728 | 57.8 | 29.0 |
| **TOTAL** | | **2,524** | **200.3** | **94.5** |
| SPY, QQQ, IWM | — | _broad market — no sector to tilt_ | | |
| TLT, GLD | — | **structurally zero — no issuer, no Form 4, ever** | | |

Top issuer SIC codes by code-P event count, which is where the problem becomes visible:

| Events | SIC | Description |
|---:|---|---|
| 131 | 6022 | State Commercial Banks |
| 141 | 2834 | Pharmaceutical Preparations |
| 79 | 6798 | Real Estate Investment Trusts |
| 58 | 6792 | Oil Royalty Traders |
| 51 | 6021 | National Commercial Banks |
| 47 | 3841 | Surgical & Medical Instruments |
| 46 | 7372 | Services — Prepackaged Software |
| 45 | 1311 | Crude Petroleum & Natural Gas |
| 40 | 6331 | Fire, Marine & Casualty Insurance |

**Method caveats, stated plainly.** SIC is a coarse proxy for GICS and for actual ETF
membership — an issuer's SIC code says what it does, not whether the ETF holds it, and the two
diverge badly (see point 4 below). The `≥$100k` column aggregates all code-P notional for one
issuer on one filing day. 29% of events could not be sector-classified because SEC throttled
the issuer-metadata lookups (453 HTTP 429s from `data.sec.gov`, then 187 HTTP 503s from the
`browse-edgar` fallback); those are shown as their own row rather than redistributed, so every
sector count here is a **lower bound**, and scaling them up proportionally would not change any
conclusion. 2026Q2 is also a seasonally *quiet* quarter for grants but a fairly normal one for
purchases.

### Reading that table

Raw volume looks adequate — ~200 code-P events per week market-wide, ~95 of them above a
$100k notional floor. The failure is in the **distribution**, not the count:

1. **Two of the 16 instruments can never receive a signal.** TLT (Treasuries) and GLD (gold)
   have no issuers and therefore no Form 4 filings, ever. Any conviction score for them is
   structurally undefined — not sparse, undefined.
2. **Three more receive no *sector* signal.** SPY, QQQ and IWM are broad-market. An
   insider-buy aggregate for "the whole market" is Seyhun's aggregate signal, which is a
   6–12 month macro indicator (see below), not a weekly tilt.
3. **The two narrow sector ETFs starve outright.** ITA gets **0.5 qualifying buys per week** —
   one every two weeks. SMH gets **1.1**. Over Rotunda's four-session window the *expected*
   count is **0.4 for ITA and 0.9 for SMH**. XLU (1.7/week) and XLP (2.2/week) are barely
   better. For roughly half the tradeable universe, the modal four-session observation is
   **zero qualifying insider purchases**, and a conviction score computed from zero
   observations is not a weak signal — it is an undefined one that will be filled by whatever
   the default is.
4. **The one sector with abundant data is the one where the data is most misleading.** XLF
   leads by a wide margin (16.6 qualifying buys/week, ~2.4× the next sector), but the SIC
   breakdown shows where those come from: State Commercial Banks (131 events), REITs (79),
   Oil Royalty Traders (58), National Commercial Banks (51), Fire/Marine/Casualty Insurance
   (40). These are overwhelmingly sub-$500M community banks and trusts. **XLF's actual top
   holdings are BRK.B, JPM, V, MA and BAC.** We would be forming a directional tilt on
   mega-cap financials from the buying behaviour of small regional lenders with essentially no
   economic overlap with the index. The abundance is an illusion created by SIC grouping; the
   signal and the instrument are not measuring the same companies.

The corroborating cross-check: of 571 distinct buy tickers in the trailing 30 days, roughly
**64% were micro/nano-cap**, and only ~14% were recognisably large-cap. Across the whole of
2026Q2, only **79 of 500 S&P 500 members (15.7%)** had any code-P buy at all — a mean of
**2.9 S&P 500 names per day** market-wide, across all eleven sectors combined.

So the honest count for the sectors that matter is: **a handful of large-cap-relevant buys per
sector per week, zero for five of the sixteen instruments, and near-zero for four more.** Over
four sessions, most sectors will see no qualifying large-cap insider purchase whatsoever.
There is nothing to differentiate.

### Is the effect strong enough at the sector level at all?

No — and this is the more fundamental objection, independent of counts.

**The insider's edge is firm-specific, which is precisely the component aggregation destroys.**
The direct published evidence is **Piotroski & Roulstone (2004), *The Accounting Review*
79(4), 1119–1151**, which measures return synchronicity (the share of a stock's variance
explained by market and industry factors) and asks which informed party pushes which kind of
information into prices:

> "stock return synchronicity is positively associated with analyst forecasting activities,
> consistent with analysts increasing the amount of **industry-level** information in prices…
> **In contrast, stock return synchronicity is inversely related to insider trades, consistent
> with these transactions conveying firm-specific information.** … insider and institutional
> trading accelerate the incorporation of the **firm-specific component alone** of future
> earnings news into prices, while analyst forecasting activity accelerates both the industry
> and firm-specific components."

Analysts carry industry information; insiders carry idiosyncratic information. Averaging N
noisy firm-specific signals within a sector shrinks the idiosyncratic edge toward zero and
leaves the common component — and the common component is not where the insider advantage
lives. Rotunda's aggregation step is a diversification operation applied to the one thing that
does not survive diversification.

Supporting evidence, same direction:

- **Alldredge & Cicero (2015), *JFE* 115(1), 84–101.** The one well-identified case of insiders
  trading on non-firm-specific information is supply-chain-specific and **sell-side only**:
  *"insiders appear to sell their own stock profitably based on public information about their
  principal customers… **We do not find similar patterns for insider purchases.**"*
- **Seyhun (1988), *Journal of Business* 61(1), 1–24**, points the wrong way for us: *"The
  evidence suggests that insiders cannot always distinguish between the effects of firm-specific
  and economywide factors."* Part of the aggregate correlation comes from insiders
  *misattributing* market moves to their own firm — a mispricing/contrarian mechanism, not a
  sector-forecasting one.

**Where an aggregate signal does work, the horizon is 6–12 months, not four sessions:**

- **Seyhun (1992), *QJE* 107(4), 1303–1331:** aggregate net insider purchases predict *"up to
  60 percent of the variation in **one-year-ahead** aggregate stock returns"* — in-sample,
  non-overlapping, over a 15-year sample (~15 independent observations; treat the 60%
  accordingly).
- **Chowdhury, Howe & Lin (1993), *JFQA* 28(3), 431–437** is the direct rebuttal: predictive
  content is *"slight"*, market returns have *"substantial influence on aggregate insider
  purchases/sales"* (causality runs mostly the other way), and *"investors cannot use aggregate
  insider transactions to profitably predict future market returns over the following **eight
  weeks**."*
- **Lakonishok & Lee (2001)** on aggregate timing: *"Insider trading activity seems to have
  little explanatory power when it comes to predicting market returns over a short horizon such
  as three months."* Predictive power appears at 12 months, with α₁ = 0.22, t = 2.09 — their own
  words, *"only marginally statistically significant."*

**And the large-cap result is the killer.** In Lakonishok & Lee, the 12-month spread between
top- and bottom-decile aggregate-insider months is **19% for small companies (significant)** and
**5% for large companies (not significant)**. Rotunda's instruments are cap-weighted large-cap
ETFs. The measured aggregate edge lives in exactly the names these ETFs exclude.

**Most of what remains is sector mean-reversion wearing insider data as a costume.** Lakonishok
& Lee, Table 5, prior-5-year-ranked NPR quintiles:

| NPR quintile | Prior 12m return | Post 12m return |
|---|---|---|
| Lowest (insiders selling) | **+38.1%** | +5.8% |
| Highest (insiders buying) | **−1.7%** | +20.6% |

Insider buying fires after a flat-to-negative year; insider selling fires after a +38% year.
Adding a prior-return control cuts their aggregate coefficient from 0.31 (t=3.46) to 0.22
(t=2.09), and they state plainly that without it *"the importance of insider trading in
predicting market returns is substantially overstated."*

**One study supports sector aggregation, and it is fragile.** Launhardt (2019), an unpublished
Ulm University doctoral thesis, aggregates Form 4 trades into the Fama-French 10-industry
scheme and reports out-of-sample R² up to 19.8% (Non-durables) and 25.1% (Shops) at h=12
months. But: it is unpublished; the regressions are **univariate with no control for past
industry returns** (the exact control that halves the effect elsewhere); the out-of-sample
window is Jan 2007–Dec 2017, dominated by one crash and the subsequent bull market; returns
are heavily overlapping; the h=1-month column is mostly insignificant; and the industry mapping
fits Rotunda badly — **Energy shows no predictability at any horizon** (relevant to XLE) and
the "Other" bucket containing financials has an OOS R² of **−35% at 12 months** (relevant to
XLF). Nothing in it supports a weekly tilt.

Finally, the market's own verdict: **no insider-driven sector-rotation ETF or index appears
ever to have existed.** The products that did exist used insider data for *stock selection*
with an explicit per-sector cap — Sabrient/Guggenheim NFO capped sector weight at 20%,
i.e. deliberately *not* sector rotation — and both it and Direxion KNOW (liquidated 2020-10-23)
are defunct. For an idea this old and this cheap to run, that absence is weak but real negative
evidence.

### Verdict

**Do not use Form 4 as a sector conviction input for Rotunda.**

The engineering is excellent and none of the objections are technical. EDGAR gives sub-minute
latency, clean structured XML with the ticker already in the document, an official free
CIK→ticker map, a maintained parser, complete free historical panels, and an explicit written
policy blessing 10 req/s of scripted access. On the access axis Form 4 beats congressional
STOCK Act disclosures decisively — 2 business days versus 45, ~800 filings/day versus a
trickle, official structured XML versus scraped PDFs. If the question were only "is this feed
usable," the answer would be an easy yes.

It fails on four independent grounds, any one of which is sufficient:

1. **Aggregation targets the wrong variance component.** Piotroski & Roulstone (2004) is
   direct published evidence that insider trades convey firm-specific information while
   *analysts* convey industry information. Averaging to a sector diversifies away precisely
   the component insiders are informed about. This is not a tuning problem; it is the design.
2. **The horizon is wrong by one to two orders of magnitude.** Every credible aggregate
   estimate lives at 6–12 months. Chowdhury et al. found it unprofitable at *eight weeks*;
   Lakonishok & Lee found "little explanatory power" at *three months*. Rotunda's window is
   four sessions. Even the firm-level filing-date pop — mean +1.0% but **median +0.2%** over
   five sessions — needs dozens of independent draws to realise a mean that skewed, and
   sector aggregation gives us far fewer than that.
3. **The large-cap universe is where the effect is absent.** Lakonishok & Lee: 19% small-cap,
   **5% and insignificant in large caps.** The FTSE-350 test that deliberately excludes small
   caps found CAR[t+1,t+20] of **−0.812%, insignificant**. And Jeon & Sulaeman (2024) found
   insider purchases in high-options-activity stocks produce *negligible* abnormal returns —
   liquid options are a marker for the null.
4. **The signal does not reach 5 of 16 instruments and is misdirected for the largest.**
   Measured, not assumed: TLT and GLD can never have a Form 4; SPY/QQQ/IWM have no sector to
   tilt; ITA expects **0.4** qualifying buys per four-session window and SMH **0.9**; and the
   one data-rich sector, XLF at 16.6/week, is populated by community banks, REITs and oil
   royalty trusts that share essentially no constituents with the mega-cap index we would
   trade.

To be precise about what is *not* being claimed: the short-window filing reaction is not a
myth. It is +1.89% CAR[0,+2] in Brochet's post-SOX sample and mean +1.0% at five sessions in
modern data at t≈5. The genuine long-horizon edge is also real — roughly 50–90 bp/month
value-weighted for a *filtered* signal (opportunistic + cluster + officer), monthly rebalance,
equity-sized. What fails is the specific conjunction Rotunda needs: **harvestable in four
sessions, aggregated to sector, in large-cap ETFs, with enough cross-sectional spread to
differentiate 16 instruments.** No study supports that conjunction, and the one paper that
tested the fast version directly (Oenschläger & Möllenhoff 2025) found returns vanish or turn
negative once sized, *before* costs.

If Rotunda's judged window were four *months* rather than four *sessions*, and the universe
were small-cap tilted, this verdict would flip. It is not, so it does not.

**If it is built anyway** — as a low-weight input rather than a primary driver — the minimum
defensible construction is: filter to opportunistic (non-routine) insiders per Cohen-Malloy-Pomorski;
require officer or director, exclude 10% owners; exclude `aff10b5One = 1`; weight by dollar
notional with a $100k floor; restrict to issuers actually held by the target ETF rather than
merely sharing its SIC code; accumulate over a trailing 6–12 month window rather than a week;
and **orthogonalise against trailing sector return**. Then benchmark it against the naive rule
"overweight the sector with the worst trailing 12-month return." If it does not beat that, it
is sector mean-reversion with extra steps.

---

## Appendix: reproduction commands

All verification used:

```bash
UA="Rotunda Research ianwalmsley@ianlan.net"
```

```bash
# Live Form 4 feed (dedupe on accession)
curl -s -H "User-Agent: $UA" \
  "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=100&output=atom"

# Daily index (after ~22:00 ET)
curl -s -H "User-Agent: $UA" \
  "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260827.idx"

# Full-text search, one day of Form 4
curl -s -H "User-Agent: $UA" \
  "https://efts.sec.gov/LATEST/search-index?q=&forms=4&startdt=2026-08-27&enddt=2026-08-27"

# One filing: list documents, then fetch the ownership XML
curl -s -H "User-Agent: $UA" \
  "https://www.sec.gov/Archives/edgar/data/1053572/000111192826000163/index.json"
curl -s -H "User-Agent: $UA" \
  "https://www.sec.gov/Archives/edgar/data/1053572/000111192826000163/wk-form4_1787862464.xml"

# CIK -> ticker
curl -s -H "User-Agent: $UA" "https://www.sec.gov/files/company_tickers.json"

# CIK -> SIC / exchange  (note the different Host)
curl -s -H "User-Agent: $UA" "https://data.sec.gov/submissions/CIK0000001800.json"

# Quarterly bulk panel (scrape the landing page for the current path)
curl -s -H "User-Agent: $UA" \
  "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip"
```

The sector-count analysis in [Verdict for sector aggregation](#verdict-for-sector-aggregation)
is reproduced by `docs/research/form4_sector_counts.py`, which expects the extracted DERA
2026Q2 TSVs plus a CIK→SIC map.

**Operational warning learned the hard way:** `data.sec.gov` enforces a *stricter* limit than
the documented 10 req/s for the `/submissions/` endpoint. Eight concurrent workers spaced at
0.125s produced **453 HTTP 429s out of 1,161 requests**. The `www.sec.gov` `browse-edgar`
fallback then returned HTTP 503 under similar load. For any bulk metadata enrichment, stay at
or below ~3 req/s with exponential backoff, cache aggressively, and prefer the bulk DERA files
or the `sics` field already present in full-text-search results over per-company lookups.

### Primary sources

- SEC fair access policy — <https://www.sec.gov/os/webmaster-faq>
- Accessing EDGAR data — <https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>
- Insider Transactions Data Sets (DERA) — <https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets>
- EDGAR filing counts by form type — <https://www.sec.gov/data-research/sec-markets-data/number-edgar-filings-form-type>

### Key papers

- Jeng, Metrick & Zeckhauser (2003), *REStat* 85(2), 453–471 — <https://direct.mit.edu/rest/article/85/2/453/57400>
- Brochet (2010), *The Accounting Review* 85(2), 419–446 — <https://publications.aaahq.org/accounting-review/article-abstract/85/2/419/3237/>
- Lakonishok & Lee (2001), *RFS* 14(1), 79–111 — <https://www.lsvasset.com/pdf/research-papers/Insider-Trades-Informative.pdf>
- Cohen, Malloy & Pomorski (2012), *JF* 67(3), 1009–1043 — <https://www.nber.org/system/files/working_papers/w16454/w16454.pdf>
- **Piotroski & Roulstone (2004), *TAR* 79(4), 1119–1151** — <https://doi.org/10.2308/accr.2004.79.4.1119>
- Oenschläger & Möllenhoff (2025), *Finance Research Letters* 72, 106514 — <https://www.sciencedirect.com/science/article/pii/S1544612324015435>
- Jeon & Sulaeman (2024), *JCF* 87 — <https://www.sciencedirect.com/science/article/abs/pii/S0929119924000750>
- Seyhun (1992), *QJE* 107(4), 1303–1331 — <https://academic.oup.com/qje/article-abstract/107/4/1303/1846948>
- Chowdhury, Howe & Lin (1993), *JFQA* 28(3), 431–437 — <https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/relation-between-aggregate-insider-transactions-and-stock-market-returns/C2B95BFA328E17AC2482A52CD1697B17>
- Alldredge & Cicero (2015), *JFE* 115(1), 84–101 — <https://econpapers.repec.org/article/eeejfinec/v_3a115_3ay_3a2015_3ai_3a1_3ap_3a84-101.htm>
- Cziráki & Gider (2021), *Review of Finance* 25(5), 1547–1580 — <https://academic.oup.com/rof/article-abstract/25/5/1547/6239716>
- Heckmann, Jacobs & Schwarz (2025), SSRN 4537187 — <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4537187>
- Launhardt (2019), Ulm thesis — <https://oparu.uni-ulm.de/bitstreams/e5327b53-3db3-4d40-a333-6fec98e370ea/download>
- Form 4 XML corpus statistics, *Scientific Data* 10:237 (2023) — <https://www.nature.com/articles/s41597-023-02147-6>
