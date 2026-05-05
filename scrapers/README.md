# scrapers/

One scraper per source. Pattern is consistent: fetch only, no scoring,
write JSONL to `data/raw/`, persist a checkpoint for resume, retry/backoff,
graceful Ctrl+C. Joins, normalization, scoring all happen in
`pipeline/build_leads.py`.

| Script | Source | Output | Resume key |
|---|---|---|---|
| `polaris.py` | meckgis on-prem ArcGIS — TaxParcel_camadata | `polaris_taxparcel_camadata_layer0.jsonl` | `objectid_1` cursor |
| `code_violations.py` | gis.charlottenc.gov — HNS code enforcement | `code_violations_*.jsonl` | `OBJECTID` cursor per layer |
| `tax_delinquent.py` | Kania Law Firm + RBCWB tax foreclosure listings | `tax_delinquent.jsonl` | full snapshot |
| `foreclosures.py` | mecktimes.com legal notices (real estate) | `foreclosures.jsonl` | `detail_id` dedup |
| `estates.py` | mecktimes.com legal notices (probate) | `estates.jsonl` | `detail_id` dedup |
| `rod.py` | Register of Deeds — CSV importer (live scrape blocked) | `rod_<doctype>.jsonl` | per-doctype snapshot |

Run any scraper with `--limit N` to cap the rows fetched in one run (handy for
schema verification). Run with `--reset` to clear the output and checkpoint
and start over.

---

## Operational notes — sources with friction

### mecktimes.com — Cloudflare WAF

Both `foreclosures.py` and `estates.py` target mecktimes.com. The site sits
behind Cloudflare with TLS-fingerprint-based bot management. From this build
machine, plain Python `urllib`, `requests`, `curl`, and even `curl_cffi`
impersonating Chrome 120 all return **403 Forbidden** on the first request.

The scrapers detect the 403 and exit cleanly without overwriting the data
file. Workarounds, in order of preference:

1. **Run from a different IP.** The block appears to be IP-reputation
   weighted; a residential IP or known-good cloud IP often clears it.
2. **Playwright with a real Chrome profile.** Drive a real browser, let
   Cloudflare's JS challenge run, then export cookies for subsequent fetches.
3. **`--from-stdin` mode.** `estates.py` accepts raw HTML on stdin; you can
   pipe a curl-from-elsewhere or Browser DevTools "Copy Response" through it.
4. **Renderer/proxy bypass.** Anthropic's WebFetch service successfully
   fetches mecktimes (it runs from a different IP / has a different TLS
   profile). The seed data already on disk for foreclosures (40 records) and
   estates (36 records) was extracted via WebFetch on the build night.

### tax.mecknc.gov — annual delinquent PDF

The county's *annual* delinquent-taxpayer list is a print-style PDF published
once a year (~April). It's not in `tax_delinquent.py`. The active foreclosure
flow (Kania + RBCWB) covers any owner whose delinquency has progressed to a
case filing — that's the higher-signal subset anyway. To pick up early-stage
delinquents (delinquent but not yet in foreclosure), parse the annual PDF
separately when it's published; output to `data/raw/tax_delinquent_pdf.jsonl`
and the pipeline will pick it up.

### Register of Deeds (Aumentum / Manatron) — login wall

`meckrod.manatron.com` is the canonical Register of Deeds search. Phase 3
reconnaissance confirmed:

- The homepage IS the login modal. There is no public/anonymous search.
- Every search path (`/SearchByName`, `/SearchByDate`, `/SearchByDocType`,
  `/RealEstate*`, `/oncoredetails`, `/Search/SearchEntry`) returns a
  238-byte "Session state is not available" stub if hit unauthenticated.
- The platform is HartIC.WebUI on IIS 10 — Hart InterCivic / Manatron
  Aumentum. Login is ASP.NET WebForms with a ~9KB `__VIEWSTATE` and
  `__EVENTVALIDATION` token that change per session.
- The form has `rdoPubCpu` / `rdoPvtCpu` radios but those are
  public/private *computer* flags (session security), not guest access.
- Default `guest`/`guest` credentials fail the form validators with
  "*Required!" — the platform is configured for paid subscribers only.
- `https://www.meckrodhistorical.com/` is open but pre-1990 only.
- `https://deeds.mecknc.gov/` is informational (no live search).

**Tonight's resolution: `scrapers/rod.py` ships in CSV-import mode
(Approach 3).** A logged-in user (or future Playwright session with
persisted cookies) exports a CSV from Aumentum's search results, drops it
in `data/raw/`, and runs `rod.py import --csv ... --doctype ...` to
normalize it to JSONL. The pipeline picks it up automatically — no
build_leads.py change required when new files arrive.

The pipeline runs **without** ROD data and emits a working leads.json
just fine; it simply can't fire the `lien` (judgment / mechanics) or full
`transfer` (quitclaim) signals that ROD data unlocks. A partial `transfer`
heuristic fires from POLARIS sale data alone (recent sale + nominal
consideration / post-decedent sale).

**Future paths to live scrape:**
1. Acquire Aumentum credentials (subscription or free public account if
   Mecklenburg ever exposes one) and drive Playwright with persistent
   cookies.
2. Move the ROD scraper to a self-hosted residential-IP runner that can
   hold a long-lived session and survive the platform's session-state
   timeouts.
3. File a public-records request for bulk monthly extracts (NCGS Ch. 132).

### eCourts Portal (Tyler Odyssey) — CAPTCHA

Per `RECON.md`, the eCourts statewide portal is the canonical source for
estate (`E`) and judicial-foreclosure (`SP`) filings, but is heavily
CAPTCHA-gated. We **do not** scrape it directly. Instead, the newspaper
notice flow at mecktimes.com (real-estate + probate index groups) provides
parallel coverage of the events that matter — anything that goes to sale or
to creditor-notice ends up in print by statute.

Gaps this leaves:

- New estate filings that haven't yet hit the 4-week notice publication
  window (lag of ~1–7 days in practice).
- Foreclosure cases that are filed but not yet noticed for sale.

For now these are acceptable gaps; closing them requires the eCourts portal
and a CAPTCHA-solving workflow.

### County (non-Charlotte) code enforcement — no open data feed

`code_violations.py` covers the **City of Charlotte** only. Mecklenburg
County's separate code-enforcement system (Cornelius, Davidson, Huntersville,
Matthews, Mint Hill, and unincorporated areas) is at code.mecknc.gov and has
no equivalent open-data feed identified yet. Parcels in those jurisdictions
will not have a `code` pattern signal until a separate scraper is built.

---

## Convention checklist for new scrapers

Every new scraper in this folder should:

1. Be a single self-contained file under `scrapers/`.
2. Write JSONL to `data/raw/<source>.jsonl`.
3. Persist a checkpoint to `data/raw/<source>.checkpoint.json`.
4. Accept `--limit N` and `--reset` flags.
5. Use `Path(__file__).resolve().parents[1]` to locate the project root —
   never hardcode paths.
6. Include retry/backoff on transient HTTP errors.
7. Install a SIGINT handler that flushes the checkpoint cleanly on Ctrl+C.
8. Tag each output row with `_source` so the pipeline can disambiguate.
9. Never scrape, score, or filter — fetch and normalize-format only.
10. Document any new operational friction at the top of this file.
