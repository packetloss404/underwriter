# Congressional Trade Disclosures as a Trading Signal

**Research date: Friday, August 28, 2026**
**Status: investigated, decided — SECONDARY / corroborating input only (see final section)**

Research method note: rather than relying on tracker summaries, this report pulls **primary
sources directly** — the House Clerk's bulk disclosure index
(`disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip`, updated daily, last
modified 2026-08-28 09:00) and the Senate EFD search API
(`efdsearch.senate.gov/search/report/data/`). A decryptor/text-extractor was written for the
House's RC4-encrypted PTR PDFs and **all 368 House PTRs filed in 2026** were parsed for
ticker-level data. Those numbers are exact counts, not estimates. Statutory text was read from
uscode.house.gov; bill status from govinfo/govtrack; ETF returns from issuer fact sheets.

---

## Bottom line up front

**Data access: easy. Signal density in a 4-day window: fatal.**

Getting the data is a solved problem — there are two free, permissively-licensed, daily-refreshed
feeds that work in under 5 minutes, plus ungated primary sources. Nothing legal or technical
blocks the ingest.

The problem is arithmetic. Expect **~9–10 new PTR filings total** between Mon Aug 31 and Thu
Sep 3, 2026, carrying **~40–150 transaction lines**, concentrated in a handful of members and
names. The best peer-reviewed estimate of the disclosure-date effect is **+12 to +18 basis points
over 1–2 days**. A 15 bps effect cannot be distinguished from noise with ~10 events when a single
mega-cap's daily σ is ~130 bps. And Aug 31 – Sep 3 is close to the **worst four days in the entire
monthly filing cycle** — filings cluster on days 5–15 of the month, not month-end. Sep 8–11 would
carry roughly **2× the density**.

---

## A. Disclosure mechanics

### A1. Filing deadline — unchanged, still in force

**5 U.S.C. § 13105(l)** (formerly Ethics in Government Act § 103(l); added by STOCK Act § 6(a),
Pub. L. 112-105, Apr. 4, 2012, 126 Stat. 293). Verbatim requirement — file:

> "not later than **30 days after receiving notification** of any transaction required to be
> reported under section 13104(a)(5)(B), **but in no case later than 45 days after such
> transaction**."

The credit line shows only Pub. L. 112-105, §§ 6(a), 19(a). **No 2025 or 2026 amendments** to
§ 13105(l).

- https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title5-section13105&num=0&edition=prelim

**What triggers a PTR — 5 U.S.C. § 13104(a)(5)**, verbatim:

> "a brief description, the date, and category of value of any purchase, sale or exchange … which
> exceeds **$1,000**— (A) in real property, other than property used solely as a personal
> residence …; or (B) in **stocks, bonds, commodities futures, and other forms of securities**."

Excepted: personal residence; transactions solely between filer, spouse, and dependent children;
and **excepted/widely held investment funds** under § 13104(f)(8) (publicly traded or widely
diversified funds the filer doesn't control). This is why mutual funds and most ETFs generate no
PTR.

- https://www.law.cornell.edu/uscode/text/5/13104

Confirmed still operative in 2026 by the House Ethics Committee's current guidance — the CY2025
Instruction Guide published **April 15, 2026**, which walks through PTR due-date mechanics with
worked examples.

- https://ethics.house.gov/wp-content/uploads/2026/04/2025-Published-Instruction-Guide-4-15-2026-1.pdf
- Senate: https://www.ethics.senate.gov/public/index.cfm/financialdisclosure

Extensions are **not available** for PTRs (unlike annual FDs) — only the 30-day late-fee grace
period below.

### A2. Late-filing penalty — still $200, but it escalates

The correct cite is **5 U.S.C. § 13106(d)**, not § 13107 (§ 13107 is the former EIGA § 105 —
public access, and the $10,000 penalty for *misuse* of a filed report; unrelated).

- https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title5-section13106&num=0&edition=prelim

§ 13106(d): a filer more than 30 days late "shall … pay a filing fee of **$200**." Amendment
history runs 1978–2007 plus the 2022 recodification — **no 2025 or 2026 amendment**.

**$200 is a floor, not a flat fee.** House Ethics publishes a tier schedule specifically for PTRs:

| Occurrence | Fee |
|---|---|
| 1st late PTR | $200 regardless of how many reports were missed |
| 2nd–4th late PTRs | $200 **per month** in which a late transaction occurred |
| 5th and beyond | $200 **per late transaction** (15 late trades = $3,000) |

- https://ethics.house.gov/wp-content/uploads/2024/11/Late-Fee-Waiver-form-final.pdf

**Grace period applies to PTRs.** Statutory — § 13106(d) triggers only "more than 30 days after"
the due date. A PTR filed within 30 days of its deadline is *late but fee-free*. Waiver only "in
extraordinary circumstances," by the supervising ethics committee; House waiver requests and
dispositions are non-public. Campaign funds may not be used to pay.

**Higher tiers (unchanged):** § 13106(a) — knowing and willful falsification or failure to file →
DOJ civil action, civil penalty **up to $50,000**, plus fine under title 18 and **imprisonment up
to 1 year**. Also 18 U.S.C. § 1001 exposure.

**Enforcement reality:** zero criminal prosecutions of any member under the STOCK Act. Aggregate
assessment/collection statistics are **UNKNOWN** — neither committee publishes them. Late filing
remains routine in 2026; NOTUS reported Aug 17, 2026 that even House Ethics Chair Rep. Michael
Guest filed late
(https://www.notus.org/money/house-ethics-committee-chair-michael-guest-stock-act). The widely
circulated "~12.5% of trades filed late" figure traces to an aggregator (govgreed.com), not an
official source — **unverified**.

**Empirically, the deadline is soft and does not produce a filing spike.** Among House PTRs filed
Jul–Aug 2026 (n=512 transactions), the trade→filing lag was **median 25 days, p25 12, p75 43**;
**62% filed within 30 days**, 17% at 31–45 days, and **22% filed *later* than 45 days**
(amendments and late filers — the $200 fee is not much of a deterrent). Do not model the 45-day
cap as a clustering driver.

### A3. Legislation — no ban enacted; the data source is not at risk

**As of August 28, 2026 there is no enacted law banning or restricting congressional stock
trading.** The STOCK Act of 2012 remains unamended. Verified by enumerating all 102 measures
enacted in the 119th Congress.

#### The one bill that actually moved: H.R. 7008, "Stop Insider Trading Act" (Steil, R-WI)

| Date | Action |
|---|---|
| Jan 12, 2026 | Introduced |
| Jan 14, 2026 | **Ordered reported**, House Administration |
| Feb 3, 2026 | Reported, H. Rept. 119-479 |
| **Jul 22, 2026** | **PASSED HOUSE 232–198** (Roll Call 280) |
| Aug 6, 2026 | Read 2nd time; **Senate Legislative Calendar, General Orders, Calendar No. 548** |
| Since then | **Nothing.** No cloture filed, not scheduled, no vote |

**Status: passed one chamber only. Not enacted.**

**What's blocking it:** Section 3 of the bill is a federal voter photo-ID mandate (SAVE Act
language, new HAVA § 303A). UC requests to pass it were objected to twice (Padilla, Jul 27 and
Jul 29, 2026). Decisive arithmetic: cloture on the *clean standalone* photo-ID bill (S. 5271)
**failed 52–46 on Aug 8, 2026** — if the ID language alone can't clear 52, H.R. 7008 carrying it
can't reach 60. The Senate is in pro forma until **Sept 14, 2026**, and September floor time is
already committed to other measures.

- https://www.govinfo.gov/bulkdata/BILLSTATUS/119/hr/BILLSTATUS-119hr7008.xml
- https://www.govtrack.us/congress/votes/119-2026/h280
- https://www.congress.gov/bill/119th-congress/house-bill/7008 (canonical; congress.gov 403s
  automated fetchers — all content above verified via govinfo/govtrack/Senate Calendar)

#### H.R. 7008 is NET-ADDITIVE to disclosure

Verified directly against the engrossed text
(https://www.govinfo.gov/content/pkg/BILLS-119hr7008eh/html/BILLS-119hr7008eh.htm):

- **PTRs are fully preserved.** Zero occurrences of "periodic transaction," "103(l)," "repeal," or
  any strike directive. It adds a new subchapter alongside existing STOCK Act machinery; its only
  cross-reference to § 13104 is definitional.
- **It ADDS a new public disclosure — the advance-notice provision.** Notice of intent to sell
  must be published **7–14 calendar days BEFORE the sale**, on a website controlled by the Clerk
  of the House / Secretary of the Senate. That is *advance* signal, strictly better than a 45-day
  lag, and would be a materially stronger input than anything available today.
- **Effective date: 180 days after enactment** (Sec. 2(c)). Given Senate paralysis, earliest
  realistic effect is well into 2027.
- **Bars purchases only — no divestiture of existing holdings.** Sales (and therefore sale-side
  PTRs) continue indefinitely.

So even the worst case for the data source is not a kill — it is a reduction in buy-side equity
volume plus a new advance-notice feed.

**Press-coverage trap worth recording:** CNBC and others described H.R. 7008 as raising the
*disclosure* penalty to "$2,000 or 10%." **That is wrong.** The passed text contains zero
occurrences of "$200," "late filing," or "filing fee" and does not amend § 13106(d). The
$2,000/10% fee is a *new* § 13153 penalizing violations of the *new trading ban* — a different
offense.

#### Every other bill — introduced only, with two exceptions

| Bill | Title | Sponsor | Status |
|---|---|---|---|
| **S. 1498** | PELOSI Act / HONEST Act | Hawley (R-MO) | **Ordered reported HSGAC 8–7, Jul 30 2025**; reported & Calendar No. 294, Dec 10 2025. **No floor vote.** |
| **H.R. 9367** | Stop Lawmakers From Predicting Act | Steil | **Ordered reported** Jun 24 2026 (prediction markets, not equities) |
| **S.Res. 708** | — | Moreno (R-OH) | **AGREED TO Apr 30 2026** — Senate Rule XXXVII amended to ban *prediction-market/event contracts* only. Does not touch stock trading or disclosure. |
| H.R. 4890 | ETHICS Act | Krishnamoorthi (D-IL) | Introduced only (Aug 5 2025). **No Senate companion in the 119th.** |
| H.R. 396 | TRUST in Congress Act | **Magaziner (D-RI)** | Introduced only, 103 cosponsors |
| H.R. 5106 / S. 3649 | Restore Trust in Congress Act | Roy (R-TX) / Moody (R-FL) | Introduced only; 142 cosponsors — broadest bipartisan vehicle |
| S. 1879 | Ban Congressional Stock Trading Act | Ossoff (D-GA) | Introduced only |
| H.R. 1908 + H.Res. 725 | End Congressional Stock Trading Act | Burchett (R-TN) / Luna (R-FL) | Introduced only. **Discharge Petition 119-11 stalled at 84 of 218 signatures**, last signature Jun 11 2026 |
| H.R. 3388 | PELOSI Act (House) | Alford (R-MO) | Introduced only |

**Bill-number corrections** (these are commonly mis-cited): the PELOSI Act is **S. 1498**, not
S. 1121 (that's the Performing Artist Tax Parity Act). There is **no** S. 1734 ETHICS Act companion
(S. 1734 is the Justice for Angel Families Act). The 118th Congress ETHICS Act (S. 1171, Merkley)
was reported out of HSGAC Jul 24 2024 but **died with the 118th** — it carries no status forward.

**House rules: no change.** H.Res. 5 (119th rules package) contains zero occurrences of "stock,"
"insider," "divest," or financial-disclosure restrictions. All 154 agreed-to House resolutions
swept — nothing adopted on this subject.

CRS R48641 (updated Jul 29, 2026) lists **44 distinct measures**; 40 remain at introduction.
Mirror: https://www.everycrsreport.com/reports/R48641.html

### A4. Options disclosure — a ~1% tail, and shrinking

**Do not build an options-mirroring strategy on this data.** Computed directly from primary
filings:

| Corpus | Stock lines | Options lines | Options share |
|---|---|---|---|
| House, all 3,463 PTRs 2020–2025 | 25,903 `[ST]` (87.6%) | **535 `[OP]`** | **1.8%** (≈48:1) |
| Senate, 1,801 machine-readable eFD PTRs | 12,262 (78.3%) | **576** | **3.7%** (≈21:1) |
| House 2026 YTD (2,435 parsed lines) | 2,398 | **20** | **0.8%** |
| Combined | — | ≈1,111 of 45,246 | **≈2.5%** |

House options share by year: 1.0% (2020) → 3.3% (2021) → **5.8% (2022 peak)** → 0.9% (2023) →
1.0% (2024) → **0.35% (2025)**. Senate peaked at 18.2% in 2021 and hit **0% in 2025**.

**Concentration is extreme:** only **11 of 275** House filers and **5 of 77** Senate filers have
*ever* disclosed an options line. Tuberville (453) + Loeffler (113) = 98% of all Senate options.
Top five House filers = 91% of House options.

**Caveat:** these are *line counts, not dollars*. **Dollar-weighted options share is UNKNOWN** and
would be materially higher — options lines cluster in the $500K–$5M bands while typical stock
lines sit at $1,001–$15,000.

#### What a PTR actually shows for options — chamber asymmetry is the dominant fact

**House: free text, no dedicated fields.** The form has exactly 6 columns (owner, full asset name,
transaction type, date, date notified, amount). Options detail lives only in the Instruction Guide:

> "For options, include the name of the security, strike price, expiration date, and if
> applicable, indicate if it is a put or a call."

But in the e-filing system **that description box is labeled optional** — hence wide variation by
filer. Asset type code is `[OP]`.

- Form: https://ethics.house.gov/wp-content/uploads/2026/02/Final-CY-2025-PTR-Form-1.pdf
- Codes: https://fd.house.gov/reference/asset-type-codes.aspx

**Senate: genuinely structured.** The FD booklet requires "the complete asset name or ticker
symbol …, **the strike price, option type, and expiration date**," and eFD renders a hard
`Asset Type: Stock Option` column with machine-composed, byte-identical formatting.

- https://www.ethics.senate.gov/public/_cache/files/02ccce18-df8d-48cb-bea4-ed14b155cba6/2023-financial-disclosure-report-booklet-for-cy2022.pdf

**Contract count is NOT required in either chamber.** Some members volunteer it; most don't. The
amount bucket is the only quantity signal.

#### Real examples

House, strike/expiry but no contract count — Rep. Josh Gottheimer:
https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2023/20022412.pdf

```
JT  Microsoft Corporation (MSFT) [OP]  S  01/04/2023  02/06/2023  $500,001 - $1,000,000
    DESCRIPTION: Call options; Strike price $155; Expires 09/15/23
```

House, WITH contract count — Rep. Nancy Pelosi:
https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2021/20020106.pdf

```
SP  Micron Technology, Inc. (MU) [OP]  P  12/21/2021  $250,001 - $500,000
    DESCRIPTION: Purchased 100 call options with a strike price of $50 and an expiration
    date of 9/16/22.
```

Same form, same `[OP]` code, both compliant — one gives contracts, one doesn't.

Senate, structured — Sen. Tommy Tuberville:
https://efdsearch.senate.gov/search/view/ptr/dda61a64-82ed-4cc8-87e7-ef1d256c6565/

```
04/30/2024 | Joint | PYPL | PayPal Holdings, Inc. - Common Stock
  Option Type: Put  Strike price: $75.00  Expires: 06/21/2024
  | Stock Option | Purchase | $1,001 - $15,000
```

(eFD requires POSTing the prohibition agreement with CSRF token + cookies before the table
renders — see B2.)

### A5. Amount-range buckets

House PTR form, verbatim, in order:

1. `$1,001-$15,000`
2. `$15,001-$50,000`
3. `$50,001-$100,000`
4. `$100,001-$250,000`
5. `$250,001-$500,000`
6. `$500,001-1,000,000`  ← the missing `$` is a real typo on the official form
7. `$1,000,001-$5,000,000`
8. `$5,000,001-$25,000,000`
9. `$25,000,001-$50,000,000`
10. `Over $50,000,000`

Plus a separate trailing column: `Transaction in a Spouse or Dependent Child Asset over
$1,000,000`.

**Senate:** same ten, but the spouse/DC rule appears as an **inline 11th bucket**
(`Over $1,000,000***`) between #6 and #7.

Statutory basis 5 U.S.C. § 13104(d)(1). Under § 13104(e)(1)(F), for an **independently held**
spouse/dependent-child asset the granular top brackets are unavailable — **resolution saturates at
"over $1,000,000."** Position sizing off the bucket midpoint loses top-end resolution there.

---

## B. Data access

### B1. House Clerk — bulk ZIP, ungated, no auth, no robots.txt

**This works and is trivial.** Verified 2026-08-28:

```
GET https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip
→ 200, application/x-zip-compressed, 56,843 bytes
   last-modified: Fri, 28 Aug 2026 13:00:05 GMT   (regenerated daily)
```

2023/2024/2025FD.zip all return 200 (2025FD.ZIP = 105,284 bytes); 2027FD.zip 404s. No auth, no
cookies, no headers required.

Each ZIP contains `{year}FD.txt` (tab-delimited, 1,582 rows for 2026) and `{year}FD.xml`.
Columns:

```
Prefix  Last  First  Suffix  FilingType  StateDst  Year  FilingDate  DocID
```

`FilingType` values: `P` = PTR, `C` = candidate, `X` = extension, `W` = withdrawal, etc.
**`FilingType == 'P'` is the PTR — 368 of them in 2026**, latest FilingDate 8/27/2026.

Individual PTR PDF URL pattern (verified):

```
https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{Year}/{DocID}.pdf

e.g. .../ptr-pdfs/2026/20034496.pdf  → 200, application/pdf, 88,791 bytes
     .../ptr-pdfs/2026/20035260.pdf  → 200, application/pdf, 67,401 bytes
```

Annual FDs use `.../financial-pdfs/{Year}/{DocID}.pdf` (verified: `2026/10081549.pdf` → 200).
The year directory is required; a bad DocID 404s per-document, confirming the 404 is not
per-path.

`https://disclosures-clerk.house.gov/robots.txt` → **HTTP 404** (IIS default error page). No
robots rules at all. No click-through agreement — the prohibition notice is static text with no
checkbox and no gate.

**The catch: the index is free, the PDFs are the work.** House PTR PDFs are RC4-encrypted (a
decryptor was needed to parse the 2026 set). Roughly **5% are scanned images requiring OCR** and
yield nothing to a text extractor — in the 2026 set, **43 of 368 were paper/scanned and
unparseable** by text methods, leaving 325 machine-readable.

### B2. Senate EFD — no API, but the handshake works

**No official API and no bulk download.** `https://www.ethics.senate.gov/public/index.cfm/financialdisclosure`
(read in full) points only at `efd.senate.gov` (filer-side) and `efdsearch.senate.gov` (public
search). No bulk data, no API, no machine-readable format mentioned. Non-electronic reports are
obtainable only at the Office of Public Records kiosk (144 Hart SOB) or by paid mail request.

**Infrastructure:** Django + `Server: gunicorn` behind what looks like an F5 LB (opaque
persistence cookie). Headers: `x-frame-options: DENY`, `x-content-type-options: nosniff`,
`referrer-policy: same-origin`, HSTS. Media on separate nginx host `efd-media-public.senate.gov`.
**No Cloudflare, no WAF challenge, no captcha, no JS challenge.**

#### The exact flow, verified end-to-end 2026-08-28

**Step 1 — GET, receive CSRF:**

```
curl -s -L -c cookies.txt -A "<browser UA>" https://efdsearch.senate.gov/search/
→ 302 → /search/home/ → 200
   Set-Cookie: csrftoken=...; Max-Age=31449600; SameSite=Lax; Secure
```

The page carries `<form action="" method="POST" id="agreement_form">` with a checkbox
`name="prohibition_agreement" value="1"` and a hidden `csrfmiddlewaretoken`. **Note the form token
and the cookie token are different values** — Django masks the form token per-render. Either works
if you pass the pair consistently.

**Step 2 — POST the agreement to `/search/home/`** (the form posts to `action=""`, i.e. itself):

```
curl -s -D - -b cookies.txt -c cookies.txt \
  -e "https://efdsearch.senate.gov/search/home/" \
  -d "prohibition_agreement=1" -d "csrfmiddlewaretoken=$CSRF" \
  https://efdsearch.senate.gov/search/home/
→ 302, Location: /search/
   Set-Cookie: sessionid=eyJzZWFyY2hfYWdyZWVtZW50Ijp0cnVlfQ:...; SameSite=Strict; Secure
```

The sessionid is a Django-signed cookie whose payload base64-decodes to literally
`{"search_agreement":true}`.

**Step 3 — POST the search:**

```
POST https://efdsearch.senate.gov/search/report/data/
Cookie: csrftoken=...; sessionid=...
X-CSRFToken: <csrftoken cookie value>
X-Requested-With: XMLHttpRequest
body: start=0&length=100&draw=1
      &report_types=[11]&filer_types=[]
      &submitted_start_date=01/01/2012 00:00:00&submitted_end_date=
      &candidate_state=&senator_state=&office_id=
      &first_name=&last_name=&csrfmiddlewaretoken=<token>
→ 200, Content-Type: application/json
```

#### Two implementation findings that will otherwise cost hours

**(a) The agreement session is NOT enforced on the data endpoint.** Verified: with a fresh cookie
jar containing only `csrftoken` and **no** `sessionid`, `/search/report/data/` returned **200**
with full JSON (`recordsTotal: 2420`). Only CSRF is enforced — a POST with no token at all
returned **403**. The agreement gate *is* enforced on document views:
`GET /search/view/ptr/<uuid>/` with no cookies → **302 → /search/home/**. Do the agreement POST
anyway; it is one request and it is the intended flow.

**(b) An empty `submitted_start_date` reliably returns 503.** Reproduced four times, always in
~0.28s — too fast to be a timeout, it is a hard rejection, and it serves a "U.S. Senate: Site
Under Maintenance" HTML page that reads as an outage:

```
[report_types=[11] length=25 start=0 date=''                    ] HTTP 503
[report_types=[11] length=25 start=0 date='01/01/2012 00:00:00' ] HTTP 200  recordsTotal=2420
```

`length=5000` also returns 503. **Always pass `submitted_start_date=01/01/2012 00:00:00` and keep
`length` ≤ ~100.** This is the single biggest gotcha in the Senate path.

#### Anti-bot / rate limiting — none observed

- `robots.txt` on `efdsearch.senate.gov`: **404**. Pages carry `<meta name="robots" content="noindex">`
  (an *indexing* directive, not an access rule).
- 8 back-to-back POSTs to `/search/report/data/` with zero delay: `200 × 8`, 0.18–0.29s each. No
  429, no backoff header, no Retry-After.
- ~50 requests total across the session, no throttling, no block.
- **UNKNOWN:** whether sustained high-volume crawling (thousands of requests/hour over hours)
  eventually triggers a block — the sample was too small to rule it out. Add a courtesy delay
  (~1s) even though nothing enforces one.

#### ⚠️ Datacenter IPs are bot-blocked

Two independent codebases document Akamai/Imperva **403s on cloud egress** from
efdsearch.senate.gov; one runs from GitHub Actions instead, another uses a Mac relay. The WSL host
reached it fine (200) on 2026-08-28. **This rules out a Cloudflare Workers ingest path for the
Senate** — relevant given the stack default. Run Senate ingest from the WSL host or GitHub
Actions. The House bulk ZIP has no such issue.

#### Formats — three answers, none of them "PDF"

**Electronic filings → structured HTML.** `GET /search/view/ptr/<uuid>/` returns `text/html`,
~13KB, fully parseable. Columns rendered:
`# | Transaction Date | Owner | Ticker | Asset Name | Asset Type | Type | Amount | Comment`.
Example row (Sen. Coons):
`08/07/2026 | Spouse | -- | W.L. Gore & Associates, Inc. | Non-Public Stock | Sale (Partial) | $100,001 - $250,000`.
Annual reports at `/search/view/annual/<uuid>/` are ~129KB HTML with
`<table class="table table-striped">` per section (Part 1 Honoraria through Part 9 Agreements,
including "Part 4b. Transactions"). **No PDF anywhere in the electronic path.**

**Paper filings → scanned GIF images, one per page.** Not PDFs and not scanned PDFs — raw GIFs:

```html
<img class="filingImage"
     src="https://efd-media-public.senate.gov/media/2026/2/000/000/000000513.gif" />
```

Verified directly: 200, `Content-Type: image/gif`, 212KB, **fetchable with no cookies at all**
(the media host has no auth and no robots.txt — 404). There is also
`/search/print/paper/<uuid>/` (200, HTML, 9KB), a print-stylesheet wrapper around the same images.
**These require OCR.** The `pdfobject.js` include on the page is vestigial.

**Paper fraction, measured (not guessed).** Across all report types since 2012-01-01, sampling 598
rows at six offsets: annual 44.3%, **paper 23.4%**, ptr 23.1%, extension-notice 9.2%.

For PTRs specifically the paper share collapses over time (420 PTRs sampled at six offsets,
date-descending):

| Offset | Date range | Paper | HTML |
|---|---|---|---|
| 0 | 08/2026 – 04/2026 | 6% | 94% |
| 500 | 04/2023 – 08/2022 | 17% | 83% |
| 1000 | 11/2019 – 07/2019 | 19% | 81% |
| 1500 | 10/2017 – 06/2017 | 19% | 81% |
| 2000 | 05/2015 – 11/2014 | 11% | 89% |
| 2350 | 01/2013 – 07/2012 | **100%** | 0% |

Aggregate PTR paper share is 28.6%, but that is dominated by the 2012–13 tail before eFD existed.
A separate complete (not sampled) cross-check on **all PTRs since 2024-01-01 (n=418) gave exactly
375 HTML / 43 paper = 10.3% paper.** For recent activity, expect ~6–10% needing OCR; everything
before roughly mid-2013 is scanned images.

#### Search result row schema

`data` is an array of 5-element **positional** arrays (not keyed):

```json
["Christopher A", "Coons", "Coons, Chris (Senator)",
 "<a href=\"/search/view/ptr/5a76ceb6-2d1d-4430-8a17-6512bcb402be/\" target=\"_blank\">Periodic Transaction Report for 08/28/2026</a>",
 "08/28/2026"]
```

| idx | field | notes |
|---|---|---|
| 0 | First + middle name | inconsistent casing (`"Christopher A"` vs `"RICHARD "` — note trailing space on some legacy rows) |
| 1 | Last name | same casing inconsistency (`"BLUMENTHAL"`) |
| 2 | Office / filer label | `"Coons, Chris (Senator)"`, `"Candidate (Candidate)"`, or bare `"Senator"` on older paper rows |
| 3 | **HTML anchor** — report title + link | must be parsed; UUID and report type both live in the href |
| 4 | Date filed | `MM/DD/YYYY` |

Envelope: `draw`, `recordsTotal`, `recordsFiltered`, `data`, `result: "ok"`. Standard DataTables
server-side paging via `start` / `length`.

**Report type codes** and totals since 2012-01-01:

| code | type | total |
|---|---|---|
| `7` | Annual | 2,464 |
| `11` | Periodic Transactions | 2,420 |
| `10` | Due Date Extension | 787 |
| `14` | Blind Trusts | 22 |
| `15` | Other Documents | 5 |
| `[]` | all | **5,698** |

**Filer type codes:** `1` = Senator, `4` = Candidate, `5` = Former Senator. Plus `senator_state` /
`candidate_state` (2-letter) and `office_id`.

**Four view-path shapes** to handle when parsing column 3: `/search/view/annual/<uuid>/`,
`/search/view/ptr/<uuid>/`, `/search/view/paper/<uuid>/`, and
`/search/view/extension-notice/regular/<uuid>/` — **note the extra path segment on the last one;
it breaks a naive `/search/view/(\w+)/([0-9a-f-]+)/` regex.**

Report titles seen: Annual Report, Annual Report (Amendment), Periodic Transaction Report,
Candidate Report, Report Due Date Extension, Blind Trust, New Filer Report, Termination Report.

### B3. Is scraping permitted? — the restriction is on PURPOSE, not METHOD

The EFD home page, search page, and rendered report pages were searched for
`scrap|automat|robot|crawl|bot |spider|excessive|denial of service|acceptable use|terms of use` —
**zero matches**. There is no rate-limit clause, no automated-access clause, and no "you may not
use robots or scripts" language anywhere on the Senate EFD system. The `senate.gov` robots.txt
(served at `ethics.senate.gov/robots.txt`, 200) is a legacy named-bot blocklist of 22 agents
(`os-heritrix`, `NPBot`, `BaiDuSpider`, `psbot`, `MSIECrawler`, …) with **no `User-agent: *` rule
at all**.

Full text of the interstitial at `/search/home/`, quoted exactly:

> **Get Access**
> You must agree to the following to be able to search reports.
> Title 1 of the Ethics in Government Act of 1978, as amended, 5 U.S.C. app. § 105(c), states
> that:
> It shall be unlawful for any person to obtain or use a report:
> - for any unlawful purpose;
> - for any commercial purpose, other than by news and communications media for dissemination to
>   the general public;
> - for determining or establishing the credit rating of any individual; or
> - for use, directly or indirectly, in the solicitation of money for any political, charitable,
>   or other purpose.
>
> The Attorney General may bring a civil action against any person who obtains or uses a report
> for any purpose prohibited in paragraph (1) of this subsection. The court in which such action
> is brought may assess against such person a penalty in any amount not to exceed $10,000. Such
> remedy shall be in addition to any other remedy available under statutory or common law.
>
> *I understand the prohibitions on obtaining and use of financial disclosure reports.*

The House posts substantively identical language at
`/FinancialDisclosure/ViewSearch`, same statute, same media carve-out, but **displayed as static
text with no checkbox and no gate**:

> It is unlawful to use the information contained in these Financial Disclosure Statements for
> (A) any unlawful purpose, (B) any commercial purpose, other than by news and communications
> media for dissemination to the general public, (C) determining or establishing the credit rating
> of any individual, or (D) use, directly or indirectly, in the solicitation of money for any
> political, charitable, or other purpose. See 5 U.S.C. app. § 105(c)(1),(2).
>
> In conformity with 2 U.S.C. § 104e(b)(3), certain personally identifiable information in these
> reports, not required to be disclosed, has been redacted.

**Reading:** the automation itself is unrestricted, unmetered, and technically trivial. The live
legal question is not "may we automate?" but **"is the use commercial within § 105(c)(2)?"** A
free public-interest transparency use is squarely within historical practice — STOCK Act § 8(a)
affirmatively mandates online publication of this data, and public scrapers have operated for
years. A paid product, or any lead-gen / solicitation angle, is where § 105(c)(2) and (4) bite.
That is a judgment call for the team, not a technical finding.

**Citation note:** both sites still cite `5 U.S.C. app. § 105(c)`. The Ethics in Government Act
was recodified in 2022 and this provision now sits at **5 U.S.C. § 13107(c)**. Same text, current
cite.

### B4. Third-party aggregators — the classic free path is DEAD

**⚠️ senatestockwatcher.com and housestockwatcher.com no longer resolve (NXDOMAIN).** Both S3
buckets return `AccessDenied`:

```
senate-stock-watcher-data.s3-us-west-2.amazonaws.com  → DNS OK, HTTP 403 AccessDenied
house-stock-watcher-data.s3-us-west-2.amazonaws.com   → DNS OK, HTTP 403 AccessDenied
```

`timothycarambat/house-stock-watcher-data` on GitHub is a **404 — repo deleted**. The bucket DNS
still resolves, so anything that appears to "work" against it is cached. **Every tutorial pointing
at housestockwatcher.com/api is stale.**

| Source | Free congress data? | Cheapest paid | ToS for a public demo |
|---|---|---|---|
| **HF `ZipLime/congress-trading`** | **YES — CC0, unlimited** | n/a | **CC0 — no restriction at all** |
| **GitHub `kadoa-org/...`** | **YES — MIT, unlimited** | n/a | **MIT — permissive** |
| Alpha Vantage | YES — free key, **25 req/day** | $49.99/mo | personal / non-commercial |
| Unusual Whales | Free **1-week** trial, 30k req/day | $150/mo | individual use only |
| Quiver Quantitative | **NO free tier** | $30/mo ($25 annual) | most restrictive in the space |
| FMP | Crippled — 25 latest only, no pagination | $22/mo annual | explicitly bans multi-user apps |
| Finnhub | **NO** — premium-gated | $50/mo billed quarterly ($150 min) | no redistribution |
| Capitol Trades | No API; **BFF 503, site 429** | — | do not build on it |
| Benzinga | Key-gated (401) | no published price | — |

**Confirmed to have NO congressional data** (grepped their actual fetched docs, not assumed):
Barchart OnDemand (77 endpoints, 0 hits), Intrinio (174KB docs + 2.5MB explorer, 0 hits),
sec-api.io (SEC EDGAR Form 4 only — **not** STOCK Act PTRs), Tiingo, Twelve Data (6.2MB docs),
Finage, Marketstack, Nasdaq Data Link. **Polygon.io has rebranded to massive.com** (301 redirect);
`massive.com/pricing` has 0 congress hits. **Autopilot/Dub**: `dub.money` is a 114-byte JS stub,
`api.dub.money` **does not resolve** — no public API. Smart Insider and 2iQ are quote-only.
Insider Monkey: UNKNOWN.

Per-source detail worth keeping:

- **Alpha Vantage** — verified live with the `demo` key:
  `GET https://www.alphavantage.co/query?function=CONGRESS_TRADES&symbol=AAPL&apikey=demo`
  → 200, 407,852 bytes. Top AAPL record: **traded 2026-08-13, filed 2026-08-18** — ~5 days fresh.
  Already-normalized schema with `bioguide_id`, party, state_district, `amount_min`/`amount_max`,
  `owner_code`, `transaction_type`; `datatype=csv` supported. `POLITICIAN_METADATA` (2MB) solves
  name→bioguide matching for free. Badged "Trending" not "Premium" in the docs. **But: 25
  req/day, and it requires `symbol` OR `bioguide_id` — there is no "everything filed since date X"
  firehose.** The `demo` key only works for AAPL. Paid ladder $49.99–$249.99/mo. ToS: *"for
  personal, non-commercial use"*, with commercial triggered by use *"beyond investment analysis,
  research, testing, monitoring, and any other activities that are individual in nature."*
- **Quiver Quantitative** — alive, nicest surface, **no free tier at all**. Base
  `https://api.quiverquant.com/beta/`; `/beta/live/congresstrading`, `/beta/bulk/congresstrading`,
  `/beta/historical/congresstrading/AAPL` all → **401** (exist, gated). **Free unauthenticated
  OpenAPI spec: `https://api.quiverquant.com/docs/schema.json` (200, 211KB, OpenAPI 3.0.3, 52
  endpoints)** — worth grabbing regardless. Pricing: Hobbyist $30/mo ($25 annual), Trader $75/mo,
  Commercial contact-only; both self-serve tiers marked "No Commercial Use Rights". ToS (modified
  **June 25, 2026**) §6 bars redistribution to any third party; §8 bans *"any robot, spider, or
  other automatic device"*. §6 names an escape hatch (written email permission), and their wrapper
  README publicly offers chris@quiverquant.com for exactly this. Rate limits **UNDOCUMENTED**.
- **Capitol Trades — do not build on it.** No public API. The known BFF endpoint is broken, not
  merely blocked: `https://bff.capitoltrades.com/trades` → **503** (CloudFront/Lambda error);
  `https://www.capitoltrades.com/trades` → **429** (Vercel Security Checkpoint);
  `robots.txt` itself → 429, contents **UNKNOWN**. Run by 2iQ Research, who sell the same data
  commercially.
- **Unusual Whales** — best paid API. Live OpenAPI at `https://api.unusualwhales.com/api/openapi`;
  `GET /api/congress/recent-trades` unauth → `401 {"code":"authentication_required"}`. Headers
  `Authorization: Bearer <token>` + `UW-CLIENT-API-ID: 100001`. Thirteen congress/politician
  endpoints including **`/api/congress/late-reports`** (PTRs filed past deadline) and
  `/api/politician-portfolios/holders/{ticker}`. "API Trial - Basic" is free, **one week at a
  time**, 30,000 req/day, 90-day lookback, real-time. Paid: $150 / $375 / $625+ per month (only
  the Business tier carries commercial licensing). Whether the trial needs a card: **UNKNOWN**.
  ToS page is client-rendered and unfetchable — **UNKNOWN**.
- **FMP** — free tier near-useless here. Base is `/stable/` and doc slugs differ from endpoint
  paths (`/stable/senate-trades` is the endpoint; `.../stable/senate-trading` is the doc page).
  Free tier gives **only `senate-latest` and `house-latest`, "Page Maxed to 0", max 25 responses**
  — the 25 most recent from each chamber, no pagination, **no symbol or member lookup at all**.
  250 calls/day. Cheapest useful tier Starter **$22/mo billed annually ($264/yr)**. ToS §2.2.1
  forbids integrating data into tools accessible by third parties; §2.2.2 prohibits display on
  multi-user applications *"irrespective of whether such usage is complimentary or paid."*
  Freshness **UNKNOWN** — congressional data is absent from their Cycle Times page.
- **Finnhub — out.** `stock/congressional-trading` exists but the spec entry reads
  `"freeTier": null, "premium": "Premium Access Required"`. Cheapest is Fundamental-1 at
  **$50/month/market billed quarterly = $150 minimum spend**. Official sample response shows
  2014–2015 filings.
- **Benzinga** — real but quote-only. `GET https://api.benzinga.com/api/v1/gov/usa/congress/trades`
  → **401**. Docs at
  `https://docs.benzinga.com/api-reference/calendar-api/government-trades/get-government-trades.md`
  (the `.md` suffix matters — non-`.md` URLs 403/404). Richest schema of any vendor: includes
  **committee assignments** and a direct `disclosure_url`. History from 2003, intraday delivery.
  **No published price** — `benzinga.com/apis/pricing/` → 404.

#### Free datasets

**⭐ HuggingFace `ZipLime/congress-trading` — CC0-1.0.** Verified live:

```
lastModified: 2026-08-27T21:00:08Z · license cc0-1.0 · 775,424 rows total, 15.1 MB
data/trades/trades-house.parquet  → 200, 5,206,253 bytes
data/trades/trades-senate.parquet → 200,   258,555 bytes
```

Refreshed **multiple times per day** by an automated pipeline. Configs: trades (house/senate),
holdings, liabilities, filings, features/daily-by-ticker, plus reference tables for legislators,
terms, committees, committee_members. Ships a **Delta Lake point-in-time table** (`data/pit/`) —
the correct primitive for backtesting without disclosure-lag look-ahead bias. Schema carries
`report_url` (direct link to the source House PDF) plus provenance/QA columns `extractor`,
`confidence`, `amount_quality`, `date_quality`, `ticker_source`, `superseded_by`. The full parsing
pipeline including `recipe/house_ptr_parser.py` and an OCR backfill is published in-repo.
**Caveat: 55 downloads, 0 likes — new, unproven, single-maintainer risk.**

**⭐ GitHub `kadoa-org/congress-trading-monitor` — MIT, 121 stars, daily GitHub Action**, last
commit 2026-08-28. Static JSON over raw.githubusercontent.com, verified:

```
https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json
→ 200, 4,372,006 bytes
```

Contains **exactly 5,000 records — a rolling recent window, filing_date 2026-07-01 → 2026-08-27**
(3,382 of 5,000 have a resolved ticker). Its `stats.json` reports the full corpus: **67,582 trades,
446 filers, 55,614 filings, 2011-06-20 → 2026-08-24**, sources `house_clerk` 44,690 /
`oge_executive` 13,725 / `senate_efd` 9,167 — **uniquely includes OGE executive-branch filers**.
Precomputed lag analytics (median 31 days to file, 19,379 late). **The repo contains no ingestion
code** — parsing is done by the vendor kadoa.com; it is a showcase with baked JSON. The 5,000-record
cap makes it ideal for a live agent, wrong for deep history.

**Stale/unusable:** Kaggle `shabbarank/...-inception-to-march-23` ends Mar 2023;
`lukekerbs/...` covers 2012–2024 (exact dates UNKNOWN — Kaggle's metadata API returns a
reCAPTCHA). data.world: nothing surfaced, UNKNOWN. `timothycarambat/senate-stock-watcher-data`:
last commit 2021-03-16, no license, dead S3 feed.

### B5. Open-source libraries — mostly a graveyard

**Nothing you can `pip install` does the whole job.** Two premise corrections:
`neelsomani/senate-stock-watcher` **does not exist** (the real repo is
`neelsomani/senator-filings`); `timothycarambat/house-stock-watcher-data` **is a 404**.

| Repo | Last push | License | Stars | Verdict |
|---|---|---|---|---|
| `neelsomani/senator-filings` | 2022-01-18 | **MIT** | 413 | Abandoned, but the canonical ~25-line Senate handshake everyone copies. `RATE_LIMIT_SECS = 2`; only one that handles mid-run session expiry. Explicitly refuses PDFs (`# We cannot parse PDFs`). **Vendor this.** |
| `TattooedHead/house-stock-watcher-data` | **2026-08-28** | **NONE** | 1 | Maintained, best House reference — **legally unusable, re-implement** |
| `mainfraame/clawback` | 2026-02-01 | **MIT** | 1 | Only MIT project covering both chambers + PDFs; but pdfplumber/selenium are optional imports behind `try/except ImportError` and it **degrades silently to nothing** if absent. It's an E*TRADE bot, not a library. |
| `jaywedgeworth22/Congress.Trade` | **2026-08-27** | **Apache-2.0** | 1 | TypeScript/Deno, ~1,897 commits. **The only project in any language that solves OCR** |
| `quiverquant` (PyPI 0.2.6) | 2026-05-07 | MIT | 57 | Only maintained, cleanly-licensed PyPI package in the space. Needs a $30/mo token. |
| `CapitolMarkets/capitol-markets-ingest` | — | **MIT** | — | Cleanest small MIT reference for the mechanics (TS): real `pdf-parse` on House PTRs + full Senate handshake |
| `burd5/congress_stock_trading` | 2025-07-28 | **NONE** | 32 | Abandoned-borderline |

`TattooedHead` has the cleanest correct shape: `{year}FD.zip` → `ElementTree.parse` the XML index
→ `pdfplumber.open(BytesIO(...))` + `page.extract_table()` per PTR, deps just `requests` +
`pdfplumber`. It also has genuinely useful **OCR-damage repair** regexes (a code comment notes OCR
"garbles letter case (sP, [sT], (AAPl)) and injects 'gfedc' checkbox noise") plus
`scraper/backfill_jammed.py`. No license file — **re-implement the shape, don't copy the code.**
Senate not covered.

**Traps that look right:**

- **`capitolgains` (PyPI, MIT)** — the package the web points to first. **It downloads PDFs and
  never parses them.** Deps are `requests`, `python-dotenv`, `playwright`, `appdirs` — no
  pdfplumber/pypdf/camelot/OCR anywhere; `download_disclosure_pdf()` only verifies the file is
  non-empty. v0.1.0 from 2025-01-11, never updated. You get metadata and PDFs on disk, **zero
  transactions**.
- **`congressional-trades` (PyPI v0.1.0, MIT)** — source repo `guttu44/congressional-trades` is a
  **404**, unauditable. Its own description admits House support is "Coming in v2 (PDF parsing
  required)." Do not use.
- **`kadoa-org/congress-trading-monitor`** — most-starred repo in the space and contains **no
  ingestion code at all** (deps: react, sass, govuk-frontend). Great data, not a library.

**OCR for scanned House PTRs — exactly one project has solved it, and it isn't Python.**
`jaywedgeworth22/Congress.Trade` (TypeScript/Deno, Apache-2.0) has ~40 files under
`app/src/extraction/`: `anthropicVision.ts`, `openRouterVision.ts`, `visionLlm.ts`,
`visionSubmitGuard.ts`, `textPdf.ts`, `senatePaperMedia.ts`, `docClassifier.ts`,
`extractRouting.ts`. Architecture: classify doc → route to text-PDF *or* **vision-LLM** →
guard/normalize. Covers House ZIP/XML→PDF, Senate EFD, and OGE 278-T. Everyone else punts:
`capitol-markets-ingest` says *"Paper PTRs (report_type 10) are scanned PDFs — out of scope for
v1"*; `TattooedHead` leaves those filers with `transactions: []`; `jeremiak` uses **human
transcription** (its "PDF data" step is literally `cd data && git pull`).
`jamiegl/financial-disclosure-scraper` **declares** pytesseract/opencv in `pyproject.toml` but
nothing imports them. **No pytesseract/camelot pipeline in this ecosystem works end-to-end.** Two
repos estimate image-only filings at ~5% of historical PTRs.

**⚠️ Do not vendor code from (NO LICENSE AT ALL):** `TattooedHead/house-stock-watcher-data`,
`burd5/congress_stock_trading`, `dws-data/congressional-trade-intelligence`,
`timothycarambat/senate-stock-watcher-data`, `jamiegl/financial-disclosure-scraper`,
`dannguyen/scrape-senate-financial-disclosures`, `Individual-1/go-efd`,
`daviddme/capitol-alpha-terminal`, `penguinpowernz/stonkcritter`.
`seralifatih/congress-trading-pipeline` claims MIT in its README but ships no LICENSE file.

**Clean verified licenses:** `senator-filings` (MIT), `clawback` (MIT), `quiverquant` (MIT),
`capitolgains` (MIT), `out_of_many_one` (MIT), `capitol-markets-ingest` (MIT), `Congress.Trade`
(Apache-2.0), `cta-pipeline` (**AGPL-3.0 — copyleft, avoid**). Nothing exists on CRAN.

### B6. Fastest path to working data — under 5 minutes, free

**Primary: HuggingFace `ZipLime/congress-trading` (CC0).**

```bash
pip install pandas pyarrow
curl -L -o trades-house.parquet \
  https://huggingface.co/datasets/ZipLime/congress-trading/resolve/main/data/trades/trades-house.parquet
curl -L -o trades-senate.parquet \
  https://huggingface.co/datasets/ZipLime/congress-trading/resolve/main/data/trades/trades-senate.parquet
```

No key, no signup, no rate limit, **and CC0 means zero licensing risk for a public demo** — which
no commercial API on this list offers at any free tier. It also entirely removes the
House-PDF-parsing problem, which is the single thing that kills every DIY attempt.

**Because it's a 55-download, 0-like dataset from one maintainer, do not trust it blind.**
Reconcile its filing counts against `kadoa`'s `stats.json` and against the House `2026FD.txt` PTR
count (**368 for 2026**) before building signal on it.

**Fallback A — kadoa raw JSON (MIT), one line, no dependencies:**

```bash
curl -sL https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json
```

5,000 most recent trades (Jul 1 – Aug 27, 2026), MIT, refreshed daily. **The better choice if the
agent only cares about recent signal** — live-window data, permissive license, zero setup.

**Fallback B — Alpha Vantage free key.** Best for per-symbol lookup rather than bulk; instant
self-serve key; ~5 days fresh. But 25 req/day is brutally tight for an agent.

**Do not:** build on housestockwatcher/senatestockwatcher (dead), Capitol Trades (503/429 +
commercial ToS), or `capitolgains` (downloads PDFs, parses nothing). Do not pay Finnhub $150 for
this.

**If primary-source ground truth is needed:** House ZIP + Senate EFD both work today, free, no
auth — use them as a **reconciliation layer, not the ingest path**. Vendor
`neelsomani/senator-filings`'s `_csrf()` (MIT, ~25 lines) for the Senate; follow `TattooedHead`'s
ZIP→XML→pdfplumber shape for the House, re-implemented. Run it from the WSL host or GitHub
Actions, **not from a Cloudflare Worker**.

---

## C. Signal density — the arithmetic that decided this

### C1. Filing counts (exact, from primary sources)

**PTR documents filed, July & August 2026:**

| | July 2026 | Aug 2026 (thru 8/28) |
|---|---|---|
| House | **56** | **44** |
| Senate | **12** | **26** |
| **Total** | **68** | **70** |

Monthly House PTR filings, 2026: Jan 48, Feb 38, Mar 46, Apr 41, May 52, Jun 43, **Jul 56, Aug 44**.
Monthly Senate PTR filings, 2026: Jan 19, Feb 10, Mar 16, Apr 10, May 17, Jun 12, **Jul 12, Aug 26**.

**Annual baselines (PTR documents filed):**

| Year | House | Senate | Total | ≈ per month |
|---|---|---|---|---|
| 2023 | 460 | 115 | 575 | ~48 |
| 2024 | 451 | 129 | 580 | ~48 |
| 2025 | 515 | 167 | 682 | ~57 |
| 2026 (Jan 1 – Aug 28) | 373 | 122 | **495** | ~62 → annualizes to **~735** |

2026 is running **~25–30% above the 2023–24 baseline**. Distinct filers YTD: **106 House members,
26 Senate filers**.

**Transaction lines — the trading-relevant number.** Parsing all 2026 House PTRs:

- **2,435 ticker-matched transaction lines** across 325 machine-readable filings (43 were
  paper/scanned and unparseable).
- Mean **9.2 transactions per House PTR**, **median 3**, max **223** (Rep. Julia Letlow,
  2026-01-13).
- Monthly House transaction lines 2026: Jan 507, Feb 266, Mar 285, Apr 354, May 243, Jun 268,
  **Jul 231, Aug 281**.
- Asset mix: 2,398 `[ST]` common stock, 20 `[OP]` options, ~15 other.
- Senate (May–Aug 2026, 63 electronic PTRs): 1,260 transactions, but the distribution is extreme —
  one filing (Senate candidate Alan Armstrong, 7/21) contained **703 lines**. Type mix: 1,055
  Stock, 126 Municipal Security, 37 Corporate Bond, 31 Stock Option.

### C2. Recess status

**VERIFIED.** The House Press Gallery banner read, verbatim, on 2026-08-28: *"The House is in a
district work period. Next votes are expected Monday, August 31."* The Clerk's floor proceedings
confirm: *"The next meeting is scheduled for 12:00 p.m. on August 31, 2026."*

- **House: returns Monday, Aug 31, 2026, 12:00pm.** Aug 31 – Sep 3 is an in-session week for the
  House.
  https://pressgallery.house.gov/schedules/2026-house-calendar
- **Senate: in state work period since ~Aug 10; returns Monday, Sept 14, 2026.** Roll Call
  (2025-11-19): *"The Senate will also stay in session for the first week of August but then won't
  return until Sept. 14."*
  https://rollcall.com/2025/11/19/senate-calendar-2026-midterm-election/ · corroborated by MOAA:
  https://www.moaa.org/content/publications-and-media/news-articles/2026-news-articles/advocacy/august-recess-ends-whats-next-for-congress-before-the-election/
- **Labor Day is Sept 7, 2026** — so unlike 2023/24/25, Sep 1–3 are *not* holiday-shortened.
  Markets are open Aug 31 – Sep 4.

### C3. Estimate for Mon Aug 31 – Thu Sep 3, 2026

> **~6–14 new PTR documents (central estimate ≈ 9–10), containing roughly 40–150 transaction
> lines** — with a fat right tail, since a single Cisneros- or Letlow-scale filing adds 100+ lines
> on its own.

Derivation, from the actual filing-date series (2023-01-01 → 2026-08-28, 955 weekdays, 1,651 House
PTRs):

**1. Base rate.** House averages **1.73 PTRs/weekday** over 2023–26, but **2.06/weekday in 2026
YTD** and **2.15/weekday in Aug 2026**. Senate: **0.71/weekday** 2026 YTD, **1.30/weekday** in
Aug 2026.

**2. The clustering effect runs the OPPOSITE way from intuition.** There is **no end-of-month
bunching**. Filings cluster **early in the month** and go quiet late. Weekday-normalized mean House
PTRs by day-of-month (2023–26):

| Day | 25 | 26 | 27 | 28 | 29 | 30 | 31 | 1 | 2 | 3 | … | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Mean | 1.03 | 1.00 | 0.88 | 1.13 | 0.55 | 0.93 | 0.72 | 1.10 | 1.84 | 1.55 | | **2.91** | **2.63** | **2.30** | **2.26** | **2.64** |

**The driver is the statute.** PTRs are due 30 days after *notification*, and members are notified
by **monthly brokerage statements** — so filings pile up in **days 5–15**, not at a month-end
deadline. The 45-day cap is not the binding constraint (see A2: 22% of recent filings blew past it
anyway).

**3. Applying day-of-month multipliers to the 2026 rate:**

```
Aug 31 (0.42×) + Sep 1 (0.64×) + Sep 2 (1.06×) + Sep 3 (0.89×)
  = 3.01 "average-day equivalents"
  × 2.10 House PTRs/day = 6.3 expected House PTRs
Senate: 4 days × ~0.7–1.0 = ~3–4
TOTAL central estimate ≈ 9–10
```

**4. Model calibration check:** the same model predicted **4.8** House PTRs for the week just
passed (Aug 24–27); actual was **4**. Good.

**5. Historical analogues** (last business day of Aug + first 3 business days of Sep,
House+Senate): 2023 = 3+2, 2024 = 7+2, 2025 = 8+3. Those years ran ~20% below 2026's overall rate
*and* had Labor Day falling inside the window — so scale up modestly.

**6. Recess effect is real but modest.** August 2026 House filings (44) ran below July (56), and
the Senate has been out since ~Aug 10. But filing is done by staff and accountants, not by members
on the floor, so recess dampens rather than stops it. Slight upward pressure: the House physically
returns Aug 31 and returning DC staff tend to flush a small backlog.

**7. Day-of-week:** near-flat with a slight Wed/Fri tilt (2025–26 House: Wed 178, Fri 173, Tue 169,
Thu 160, Mon 155). Weekends ~10× lower but non-zero (57 weekend filings in 2025–26). **Monday
Aug 31 is the weakest of the four days** — lowest day-of-week × lowest day-of-month multiplier.

### ⚠️ C4. The strategic point about window selection

**Aug 31 – Sep 3 is one of the thinnest windows in the entire filing cycle — roughly HALF the
density of the following week.** The same model gives:

| Window | Expected House PTRs |
|---|---|
| Aug 31 – Sep 3 | **6.3** |
| Sep 8 – 11 | **11.6** |

If a four-day window is ever movable, moving it one week is worth roughly a doubling of signal for
identical effort, for a purely structural reason.

### C5. Tickers — liquid mega-caps at the head, long thin tail

**Top tickers across all 2026 House PTRs** (2,435 parsed lines, **658 distinct tickers**):

| # | Ticker | Count | | # | Ticker | Count |
|---|---|---|---|---|---|---|
| 1 | **MSFT** | 49 | | 11 | AVGO | 17 |
| 2 | **AAPL** | 38 | | 12 | TSCO | 17 |
| 3 | **NVDA** | 30 | | 13 | T | 16 |
| 4 | **AMZN** | 29 | | 14 | PG | 16 |
| 5 | **GOOGL** | 27 | | 15 | AMD | 16 |
| 6 | IBM | 23 | | 16 | TDG | 15 |
| 7 | ABT | 20 | | 17 | MU / LLY / CHRW | 14 |
| 8 | **META** | 20 | | 18 | BRK.B / TSM / CVX / STE | 13 |
| 9 | ACN | 19 | | 19 | INTU / BA / PLTR / TTD / PANW / SCI / CRM | 12 |
| 10 | DASH / HD | 19 | | 20 | FLEX / LPLA / BRO / FSV / ENTG / HUBB / JNJ / PH | 11 |

**Senate, May–Aug 2026** (1,260 transactions): CLF 16, AAPL 14, MSFT 10, ADBE 9, INTC 8, BRK.B 6,
JPM/PYPL/GILD/NVDA 5, ORCL/CVX/GOOGL/CSX/MA/HON/JNJ/PLTR/GOOG 4.

**Verdict: overwhelmingly liquid, optionable mega-caps at the head.** The top ~25 names are all
S&P 100-scale with deep options chains and penny-wide spreads — execution and liquidity are a
non-issue there.

**But the tail is long and thin.** 658 distinct tickers across 2,435 lines means the *median*
disclosed trade is in a name appearing 1–3 times all year (TSCO, CHRW, STE, FSV, HUBB, ENTG are
mid-caps). **A strategy that fires on every disclosure spends most of its trades in the illiquid
tail; a strategy filtered to the top 25 names fires rarely.** That tension is the core design
problem with this source.

### C6. Most active filers, and roster attrition

**2026 by PTR document count —**
*House:* Kelly Morrison (MN03) 14, David Taylor (OH02) 13, Steve Cohen (TN09) 10, Cleo Fields
(LA06) 10, Diana Harshbarger (TN01) 10, Suzan DelBene (WA01) 9, Kevin Hern (OK01) 9, Mike Kelly
(PA16) 9, Tim Moore (NC14) 9, Ro Khanna (CA17) 8, Hal Rogers (KY05) 8, Rick Allen (GA12) 8, April
McClain Delaney (MD06) 8, Josh Gottheimer (NJ05) 8, Thomas Kean (NJ07) 8, Scott Peters (CA50) 8,
Michael McCaul (TX10) 7, Gil Cisneros (CA31) 7.
*Senate:* **John Boozman 20**, Tommy Tuberville 10, Dave McCormick 9, John Fetterman 8, Shelley
Moore Capito 6, Sheldon Whitehouse 6, Gary Peters 4.

By *transaction volume* rather than document count, trackers put **Ro Khanna far ahead** —
congressstock.com shows Khanna at 5,490 trades in the last 12 months, McCaul 1,143, Cisneros 1,134
(https://www.congressstock.com/). **Khanna's and Cisneros's filings are advisor-managed,
hundreds-of-tiny-lines affairs — high count, near-zero signal. Count is anti-correlated with
information here.**

**Roster attrition is material and mostly unfavorable**, verified against the Clerk index:

| Member | 2025 PTRs | 2026 PTRs | Status |
|---|---|---|---|
| **Marjorie Taylor Greene (GA14)** | 25 | **0** | ❌ **RESIGNED effective Jan 5, 2026** |
| **Mark Green (TN07)** | 33 | **0** | ❌ Resigned July 2025 |
| Nancy Pelosi (CA11) | 3 | 3 (last **2026-08-21**) | Active but low-frequency; **retiring — final filings** |
| Josh Gottheimer (NJ05) | 12 | 8 (last 2026-08-10) | Active |
| Ro Khanna (CA17) | 12 | 7 (last 2026-08-07) | Active, huge line counts |
| Michael McCaul (TX10) | 12 | 6 (last 2026-07-08) | Active |
| Gil Cisneros (CA31) | 11 | 7 (last 2026-07-02) | Active, huge line counts |
| Dan Meuser (PA09) | 2 | 5 (last 2026-07-02) | Active; incl. 5 partial NVDA sales Mar–Jul 2026 |

Two of the highest-frequency historical filers (MTG + Mark Green, **58 PTRs between them in
2025**) are gone. MTG resignation:
https://www.nbcnews.com/politics/congress/rep-marjorie-taylor-greene-resign-january-rcna245278

Pelosi's **Aug 21, 2026** filing disclosed Bloom Energy and Intel call options; she has disclosed
~$18M in transactions in 2026. She is **not seeking re-election** — after January 2027 the single
most market-moving disclosure name disappears.
https://www.benzinga.com/news/politics/26/08/61394899/nancy-pelosi-discloses-up-to-13-5-million-in-stock-options-trades-77-6x-her-annual-salary

---

## D. Evidence of edge

### D1. The classic result did not replicate

**Ziobrowski, Cheng, Boyd & Ziobrowski (2004), JFQA 39(4) — Senate, 1993–1998.** Calendar-time
portfolio mimicking Senators' purchases beat the market by **85 bps/month** (~10%+/yr); sales
portfolio underperformed by 12 bps/month. Buy-minus-sell spread ≈ 1 pt/month. Multi-month rolling
holding windows.
https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/abnormal-returns-from-the-common-stock-investments-of-the-us-senate/A39406479940758D59E09FDCB8EE9BEC

**Ziobrowski, Boyd, Cheng & Ziobrowski (2011), Business and Politics 13(1) — House, 1985–2001**,
>16,000 transactions. Purchase portfolio beat market by **55 bps/month (~6%/yr)**. Same
calendar-time methodology.
https://gwern.net/doc/economics/2011-ziobrowski.pdf

**Eggers & Hainmueller (2013), "Capitol Losses," Journal of Politics 75(2) — 2004–2008.** Direct
reversal: **Congress underperformed the market by ~2–3%/year**; most members would have done
better in an index fund. Their critique: the Ziobrowski result is fragile to model specification
(estimates swing from −10% to +20% annualized across CAPM vs Fama-French) and to portfolio
weighting (trade-weighted vs equal-weighted vs aggregate).
https://andy.egge.rs/papers/Eggmueller_CapitolLosses.pdf

**Post-STOCK Act — what actually matters for 2026:**

- **Belmont, Sacerdote, Sehgal & Van Hoek (2020), NBER w26975** — Senate 2012–Mar 2020. Purchases
  **underperform** size/industry-matched benchmarks by 11 / 28 / 17 bps at 1 / 3 / 6 months. No
  committee-linked stock-picking skill. https://www.nber.org/papers/w26975
- **Karadas (2019), Financial Review** — 2004–2010. Powerful Republicans earned >35%/yr abnormal
  returns at a 1-week horizon **pre-STOCK Act; these disappear after 2012.**
  https://onlinelibrary.wiley.com/doi/10.1111/fire.12180
- **Chen & Sacerdote (2026), NBER w35041**, "Capital in the Capitol: Congressional Trades Resemble
  Uninformed Retail Trading" — 2012–2023. Rank-and-file underperform or match; trade timing tracks
  retail social-media sentiment. **Leadership members retain positive alpha.**
  https://www.nber.org/papers/w35041
- **Wei & Zhou (2025), NBER w34524**, "'Captain Gains' on Capitol Hill" — members ascending to
  **leadership** outperform matched peers by **~47 pts/yr post-ascension**; rank-and-file show
  nothing. https://www.nber.org/system/files/working_papers/w34524/w34524.pdf

**Unusual Whales annual reports** (marketing-grade, not risk-adjusted, survivorship-affected):

- 2023: SPY +24%; Dems avg +33%, Reps +18%; **only ~1/3 of members beat the index.**
- 2024: SPY +24.9%; Dems +31.1%, Reps +26.1%; ~half of active traders individually beat it.
  https://thehill.com/business/5072670-dozens-of-lawmakers-beat-stock-market-in-2024-report/
- **2025: SPY +16.6%; Dems +14.4%, Reps +17.3% — Congress in aggregate roughly matched or slightly
  LAGGED. Only ~32% (100 of 311) beat the S&P 500.**
  https://unusualwhales.com/congress-trading-report-2025

### D2. NANC / KRUZ actual performance

Housekeeping: **KRUZ was renamed to ticker GOP effective March 21, 2025** — same fund, same CUSIP,
same Feb 7, 2023 inception. Post-2025 data is filed under **GOP**.

| Metric | NANC | KRUZ/GOP | S&P 500 TR |
|---|---|---|---|
| **CY2025 total return (NAV)** | **18.66%** | **17.16%** | **17.88%** |
| CY2024 (NAV) ⚠️ | ~26.86% | ~14.45% | UNKNOWN |
| **Since-inception annualized @ 12/31/2025** | **23.63%** | **14.75%** | **20.96%** |
| **Since-inception annualized @ 6/30/2026** | **23.58%** | **19.54%** | **21.06%** |
| Trailing 1-yr @ 6/30/2026 (NAV) | UNKNOWN | 34.13% | 22.32% |
| **2026 YTD @ 8/28/2026** | **13.92%** | **21.28%** | **13.42%** |
| Trailing 1-yr @ 8/28/2026 | ~19.5–20.8% | 26.35% | 20.29% |
| AUM | $285.7M | $93.2M | — |
| Expense ratio | 0.72–0.73% | 0.73% | 0.09% |

Sources: https://subversiveetfs.com/wp-content/uploads/2026/01/SUBVERSIVE-NANC-Fact-Sheet_12-31.pdf ·
https://subversiveetfs.com/wp-content/uploads/2026/01/SUBVERSIVE-GOP-Fact-Sheet_12-31.pdf ·
https://subversiveetfs.com/nanc/fact-sheet · https://subversiveetfs.com/gop/fact-sheet ·
https://stockanalysis.com/etf/compare/nanc-vs-spy/

**Blunt read.** Over 3½ years, **NANC beat the S&P 500 TR by ~2.5–2.7 pts/yr annualized (23.6% vs
21.0%) — before its 0.72% fee, so ~1.8–2.0 pts net of what an index fund charges.** That is a real
but modest edge, and it is **not clearly distinguishable from a mega-cap-tech factor tilt**: ~85%
of NANC's holdings overlap S&P 500 constituents and it is tech/comm-services weighted in a period
when tech won. **KRUZ/GOP underperformed the index for most of its life** (14.75% vs 20.96%
annualized at end-2025) and only closed the gap on a hot energy/defense/financials 2026.

Two funds, opposite signs, same "copy Congress" premise — **that pattern is what factor tilt looks
like, not what skill looks like.** Both funds were *down* YTD in March 2025 (NANC −5.8%, KRUZ
−1.6%).
https://www.thedailyupside.com/investments/etfs/new-gop-ticker-spotlights-politically-themed-etfs/

### D3. The disclosure-date event study — the +12–18 bps figure and its source

There is **one solid peer-reviewed event study on the *filing* date** (rather than the trade date),
and it is the single most directly relevant paper to any copy-trading design:

> **Abdurakhmonov, Snider, Ridge & Hasija (2023),** *Strategic Management Journal* **44(5):
> 1168–1198** — "Perceptions of Political Self-Dealing? An Empirical Investigation of Market
> Returns Surrounding the Disclosure of Politician Stock Purchases."

- **2,234 Senate purchase events, 2012–2020** (post-STOCK Act). Fama-French 4-factor CARs centered
  on the **public PTR disclosure date**, not the trade date.
- **CAR(0,+1) = +0.12%, p = 0.001**
- **CAR(0,+2) = +0.18%, p = 0.000**
- Effect is stronger where the Senator sits on a committee with jurisdiction over the firm's
  industry — CAR approaching **+0.5%** — amplified by firm lobbying spend and campaign
  contributions.
- **Critical caveat from the same paper: those stocks show NEGATIVE abnormal returns over the
  following 6–12 months.** The disclosure-day pop partially reverses.

https://sms.onlinelibrary.wiley.com/doi/full/10.1002/smj.3459 ·
https://walton.uark.edu/insights/posts/how-the-market-responds-to-legislator-stock-purchases.php

**What this means, stated plainly:**

1. **The market does react to the disclosure itself.** The effect is statistically real and it is
   fast (1–2 days). The thesis is not crazy.
2. **The magnitude is 12–18 basis points for the average trade.** For a liquid mega-cap you might
   pay 1–3 bps in spread plus commission, so the *gross* edge survives costs on paper — but 15 bps
   is **inside the noise band of a single stock's daily move** (MSFT's daily σ is ~130 bps). You
   would need **hundreds of independent events** to distinguish this from zero. Section C says a
   four-day window yields **~10 filings / ~40–150 transaction lines**, clustered in a handful of
   names and filers — so not even independent. **A four-day window cannot validate or refute this
   signal.**
3. **The only version with meaningful size (~50 bps) is conditional on committee-jurisdiction
   match**, which requires joining PTR data to committee assignments — real work, and it shrinks
   the event count by an order of magnitude.
4. **The effect reverses over 6–12 months.** This is explicitly *not* "buy and hold what Congress
   bought"; if anything the literature says holding is the losing half of the trade.
5. **The multi-month evidence is worse than the multi-day evidence** — the opposite of what most
   people assume going in. Ziobrowski's months-long alpha did not replicate; post-STOCK-Act
   rank-and-file trades slightly *underperform* at 1/3/6 months; the ETFs' 3½-year record is ~2
   pts/yr for one and negative-to-neutral for the other.
6. **The one durable edge in the current literature is not accessible by copy-trading at all** —
   the leadership-ascension effect (~47 pts/yr), which needs a filter on leadership/chairmanship
   status, not on disclosure volume.

**UNKNOWN / not found:** no rigorous academic quantification of the *implementation-lag drag*
(congressional trade → PTR filing → replication) on realized alpha. A ScienceDirect paper on
NANC/KRUZ (S0165176525001004) is paywalled; search-level summaries claim neither fund
significantly outperforms on a Sharpe basis, but the full text could not be verified — **do not
cite that number**. Sell & Houston, "Short-term Market Performance of Congressional Stock Trades"
(SSRN 4954641) is directly on-topic but SSRN returned 403 — **contents UNKNOWN**.

---

## Why this is a secondary input

**Decision (2026-08-28): congressional PTRs are DEMOTED from candidate primary signal to a
displayed corroborating input. Form 4 insider buying becomes the primary tilt source.**

The density arithmetic settled it. The reasoning, for the record:

**1. The window produces ~10 events against a documented ~15 bps effect.** Section C3 gives a
central estimate of **9–10 new filings and 40–150 transaction lines** over Mon Aug 31 – Thu Sep 3.
Section D3 gives the best peer-reviewed disclosure-date effect as **CAR(0,+1) = +0.12%**. Ten
events at 15 bps, in names whose daily σ is ~130 bps, is not a test — it is a coin flip with extra
steps. A four-day result in **either** direction would tell us nothing defensible.

**2. We picked structurally the worst four days.** Filings cluster on **days 5–15 of the month**,
driven by monthly brokerage statements feeding the 30-days-from-notification clock — **not** at
month-end as intuition suggests. The same model gives **6.3 House PTRs for Aug 31 – Sep 3 vs 11.6
for Sep 8–11**. Our window sits in the trough. That is a fact about the calendar, not about the
signal, and it is not fixable within the window.

**3. The Senate is out until Sept 14.** Roughly a third of the potential filing flow is
structurally absent for the whole window.

**4. The signal is directionally weakest exactly where it is densest.** The head of the
distribution (MSFT, AAPL, NVDA, AMZN, GOOGL) is liquid and optionable but also the most efficiently
priced and most crowded; the tail is 658 tickers where the median name appears 1–3 times a year and
liquidity is poor. Filtering to tradeable names collapses the event count further; not filtering
puts most trades in the illiquid tail.

**5. Filer count is anti-correlated with information.** The highest-volume filers (Khanna ~5,490
trades/12mo, Cisneros ~1,134) are advisor-managed accounts producing hundreds of tiny lines with
near-zero intent behind them. Meanwhile the genuinely high-signal names are shrinking — MTG and
Mark Green (58 PTRs in 2025 between them) are gone from Congress, and Pelosi is retiring.

**6. Options mirroring is off the table entirely.** Options are **~1% of transaction lines**
(0.35% House / 0% Senate in 2025), concentrated in under 5% of filers, and the House's
strike/expiry description field is **optional free text**. There is no reliable options stream to
mirror.

**7. The evidence of edge is real but slow, small, and conditional.** ~2 pts/yr for NANC net of
what an index charges, not clearly separable from a tech factor tilt, with its Republican sibling
underperforming on the same premise. The one large documented effect (leadership ascension, ~47
pts/yr) is not reachable by copy-trading.

**What we keep, and why:**

- **PTRs remain a displayed corroborating input.** When a Form 4 cluster and a congressional
  disclosure land on the same name, that agreement is worth surfacing — and it is legible,
  explainable, and demonstrably public-record, which has presentation value independent of alpha.
- **The ingest is cheap enough to keep regardless.** Two curl commands to a CC0 parquet (B6). There
  is no engineering argument for dropping it.
- **The advance-notice provision in H.R. 7008 is worth watching.** If it ever becomes law, sale
  intent published **7–14 days ahead** of the trade would change the character of this source
  entirely — from a 45-day-lagged confirmation to a genuine forward signal. It is stuck behind an
  unrelated voter-ID poison pill in a Senate that returns Sept 14 with its floor time booked, so
  nothing changes in 2026. Revisit if it moves.

**What would change the decision:** a window of ≥3–4 weeks (enough events to measure anything), a
window positioned on days 5–15 of a month, a committee-jurisdiction join to reach the ~50 bps
conditional effect, or enactment of the H.R. 7008 advance-notice provision.

---

## Appendix: open UNKNOWNs

- Aggregate late-fee assessment / collection / waiver statistics — not published by either
  committee.
- Whether the Senate operates a late-fee escalation schedule analogous to the House's — Senate
  guidance states the flat $200 only.
- **Dollar-weighted** options share of congressional trading (line-count share is ~2.5%).
- Whether sustained high-volume crawling of efdsearch.senate.gov eventually triggers a block —
  ~50-request sample cannot rule it out.
- Whether the Senate 503 on unbounded queries is a deliberate guard or an unhandled DB timeout
  surfaced by the load balancer.
- Text-layer quality distribution of House PDFs beyond the ~5% that are pure images.
- Unusual Whales ToS text (client-rendered) and whether its free trial requires a card on file.
- Quiver Quantitative rate limits (undocumented anywhere).
- FMP and Finnhub PTR-to-availability latency; FMP month-to-month pricing; Benzinga pricing;
  Capitol Trades robots.txt (429).
- Kaggle dataset last-updated dates (reCAPTCHA-walled); data.world contents; Insider Monkey.
- SSRN 4954641 (Sell & Houston, short-term performance of congressional trades) — 403, contents
  unread.
- ScienceDirect S0165176525001004 (NANC/KRUZ Sharpe analysis) — paywalled; do not cite the
  search-summary figure.
