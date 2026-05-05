# RECON — Mecklenburg County, NC Motivated Seller Intelligence

> Phase 1 reconnaissance. No code yet — this is the source map and the proposed pattern stack the build will follow.

Mecklenburg County (Charlotte, NC) is structurally different from the Texas counties the framework spec was built around. The most important differences:

- **NC is a judicial-foreclosure state.** Foreclosures are filed in Clerk of Superior Court as Special Proceedings (SP) cases. There is no equivalent of the TX trustee-sale notice that gets recorded as a `TRSALE` doc type at the county clerk. The relevant signal is a `Notice of Hearing on Foreclosure of Deed of Trust` filed in Clerk of Superior Court.
- **NC uses Deeds of Trust, not mortgages.** The lien instrument recorded at Register of Deeds is a "Deed of Trust" (DOT). Same economic role, different label.
- **eCourts is statewide.** As of October 13, 2025, all 100 NC counties run on Tyler Odyssey via the eCourts Portal. Court records (foreclosures, estates, civil judgments) all live in a single statewide system, not the county clerk.
- **Register of Deeds is property-only.** It records deeds, deeds of trust, plats, POAs, separation agreements, assumed names. It does **not** hold court filings — those are in eCourts.
- **Heirs property is a real NC distress signal.** Partial-interest and probate situations are exactly Quentin's specialty and the NC equivalent of the TX "probate + LP combo."

---

## SOURCE INVENTORY

### 1. Mecklenburg County GIS — POLARIS / Open Mapping

| Field | Value |
|---|---|
| Public viewer | https://polaris3g.mecklenburgcountync.gov/ |
| ArcGIS REST root | https://polaris3g.mecklenburgcountync.gov/polarisv/rest/services |
| Bulk open data | https://maps.mecknc.gov/openmapping/data.html |
| Data center | https://gis.mecknc.gov/data-center |
| GeoPortal (alternative UI) | https://mcmap.org/geoportal/ |
| Access method | ArcGIS REST API + bulk download (shapefile/CSV) |
| Update frequency | Parcels refreshed regularly; county explicitly publishes data under open license |
| Auth | None |
| Anti-bot | None on REST endpoint (open ArcGIS) |
| Known blockers | ArcGIS REST root currently returns 403 to bare WebFetch UA; needs a real client (curl/python with browser-like headers). Service is `polarisv` and runs ArcGIS Server 10.81. |
| Notes | This is the master parcel/owner/address table — the equivalent of HCAD/BCAD in the TX framework. Everything else gets joined back to a parcel ID (PID) here. |

**Action for Phase 2:** enumerate `/polarisv/rest/services` with a real HTTP client. Confirm the parcel `FeatureServer` or `MapServer/N` layer ID, then pull the schema. Field-name discovery is the single biggest unknown — different from HCAD's `mail_addr_1` / `str_num`.

---

### 2. Register of Deeds (real estate records)

| Field | Value |
|---|---|
| Main | https://deeds.mecknc.gov/ |
| Public search (Aumentum) | https://meckrod.manatron.com/ |
| Historical search (pre-1990) | https://www.meckrodhistorical.com/ |
| Data dashboard | https://mecklenburg-county-data-dashboard-meckgov.hub.arcgis.com/pages/register-of-deeds |
| Access method | Web search UI (Aumentum platform); no public bulk download |
| Coverage | March 1990 → present online |
| Update frequency | Same-day for new recordings |
| Auth | None for basic search |
| Anti-bot | Aumentum is session-state heavy. Expect rate limits and form tokens. |
| Doc types we care about | DEED, DEED OF TRUST, ASSIGNMENT OF DOT, SUBSTITUTION OF TRUSTEE, NOTICE OF SUBSTITUTE TRUSTEE SALE, AFFIDAVIT, QUITCLAIM, MEMO OF JUDGMENT, MECHANICS LIEN |
| Known blockers | Aumentum (Manatron) often blocks headless Chrome by user-agent. Will likely need Playwright with a real Chrome profile + per-session pacing. |
| Notes | Register of Deeds in NC does **not** hold lis pendens or court filings — those are at Clerk of Superior Court via eCourts. |

---

### 3. Tax Collector — bills, delinquencies, tax foreclosures

| Field | Value |
|---|---|
| Main | https://tax.mecknc.gov/ |
| Tax bill lookup (search UI) | https://taxbill.co.mecklenburg.nc.us/publicwebaccess/ |
| Tax bill results page | https://taxbill.co.mecklenburg.nc.us/publicwebaccess/BillSearchResults.aspx |
| Delinquent taxpayer list | https://tax.mecknc.gov/services/Delinquent-Taxpayer-Lists |
| Tax foreclosure properties | https://tax.mecknc.gov/services/tax-foreclosure-properties |
| In Rem foreclosure info | https://tax.mecknc.gov/service/rem-foreclosures |
| Access method | ASP.NET WebForms search (`.aspx`) for bills; published list (PDF/Excel) annually for delinquents |
| Update frequency | Tax bills nightly; delinquent list published yearly around April; foreclosure list updated as cases progress |
| Auth | None |
| Anti-bot | Standard ASP.NET — needs session viewstate handling |
| Known blockers | Delinquent list is published as a newspaper-style PDF (annual). Need OCR/parser. Per-bill lookup requires PID/owner — fine for enrichment but not for cold discovery. |
| Notes | NC tax foreclosure happens in two flavors — traditional judicial (Mortgage-style, GS 105-374) and **In Rem (GS 105-375)**, which is faster and Mecklenburg uses heavily. The In Rem list is a high-value distress signal — often pre-auction. |

---

### 4. Clerk of Superior Court — judicial foreclosures, estates, judgments

| Field | Value |
|---|---|
| eCourts Portal (Tyler Odyssey) | https://portal-nc.tylertech.cloud/ |
| eCourts info page | https://www.nccourts.gov/ecourts |
| Foreclosures help | https://www.nccourts.gov/help-topics/housing/foreclosures |
| Mecklenburg location | https://www.nccourts.gov/locations/mecklenburg-county |
| Special Proceedings Desk | Mecklenburg Courthouse, 3rd floor, 832 E 4th St, Charlotte (704-686-0460) |
| Public notices (newspaper) | https://mecktimes.com/ (Mecklenburg Times — required statutory advertising) |
| Access method | Portal search UI (statewide Tyler Odyssey); no bulk export |
| Coverage | All NC counties from October 13, 2025 onward; Mecklenburg from Oct 9, 2023 |
| Pre-Oct-2023 records | Email request to `Mecklenburg.ESP@nccourts.org` |
| Auth | Anonymous public search supported |
| Anti-bot | Tyler Odyssey portals are notoriously CAPTCHA-gated and rate-limited. Plan for human-in-loop or commercial proxy. |
| Case types we care about | **SP** (Special Proceedings — foreclosures, partition, condemnation), **E** (Estates — probate), **CV/CVD/CVS** (Civil — judgments that become liens), **CVM** (small claims judgments), **CR** (criminal — sometimes triggers asset forfeiture) |
| Known blockers | (a) Portal CAPTCHA. (b) No bulk export — must query by name/case number. (c) Foreclosure cases are SP-numbered but the actual sale notice is filed as a separate sub-document. (d) Statewide system means filtering to Mecklenburg requires correct location code on every query. |
| Workaround | Newspaper public notices (`mecktimes.com`) are statutorily required for foreclosure sales — scrape these as a backup signal source independent of the portal. |

---

### 5. Probate / Estates

| Field | Value |
|---|---|
| Same as #4 | eCourts Portal — case prefix `E` |
| Why this matters | NC heirs-property and partial-interest situations are Quentin's wholesale specialty. New estate filings = motivated heirs, often out-of-state. |
| Access method | Same Portal UI, filter by case type `E` |
| Update frequency | Real-time as filed |
| Anti-bot | Same Tyler Odyssey constraints as #4 |
| Known blockers | Estate filings rarely include a property address — must join to POLARIS by decedent name. Expect lower fill rate than TX (where probate often references property in the petition). |

---

### 6. Code violations — City of Charlotte + County Code Enforcement

| Field | Value |
|---|---|
| Charlotte open data portal | https://data.charlottenc.gov/ |
| Code Enforcement Cases (all) | https://data.charlottenc.gov/datasets/charlotte::code-enforcement-cases-all/ |
| Orders to Demolish | https://data.charlottenc.gov/datasets/0d519426fca841dba3646e7fc02c6ebf |
| County Code Enforcement (towns + unincorporated) | https://code.mecknc.gov/ |
| County public records portal | https://code.mecknc.gov/site-menu/public-records |
| Access method | ArcGIS Hub REST API (Charlotte data is on Esri ArcGIS Hub — direct CSV/GeoJSON download supported) |
| Update frequency | Charlotte open data refreshed regularly; check dataset metadata for cadence |
| Auth | None |
| Anti-bot | None (open data portal) |
| Coverage gap | City of Charlotte is in the Charlotte dataset. Cornelius/Davidson/Huntersville/Matthews/Mint Hill + unincorporated areas are handled by **County** Code Enforcement (separate, no equivalent open data feed identified yet). |
| Notes | "Orders to Demolish" is the highest-signal code dataset — these are condemned/condemnable structures. Combine with parcel join to surface owners. |

---

### 7. Other signals worth scoping (lower priority for Phase 2)

- **Charlotte Housing Code complaints** — separate complaint stream; check `data.charlottenc.gov`
- **Mecklenburg County permits** — high permit activity = renovation flips; absence of permits during ownership change can hint at distress
- **GIS-derived flags** — parcels in floodplain, parcels with no improvements (vacant land), parcels with mailing address ≠ site address (absentee owner). All derivable from POLARIS layers.

---

## ANTI-BOT / OPERATIONAL BLOCKERS — SUMMARY

| Source | Blocker level | Mitigation |
|---|---|---|
| POLARIS REST | Low | Use real HTTP client with browser headers |
| Open Mapping bulk | Low | Direct download |
| Charlotte open data | Low | ArcGIS Hub REST API |
| Tax bill ASP.NET | Medium | Session/viewstate handling (Playwright or `requests` with viewstate parser) |
| Register of Deeds (Aumentum) | High | Playwright with real Chrome profile; pace requests |
| eCourts Portal (Tyler Odyssey) | **Highest** | Likely human-in-loop; plan for CAPTCHA. Use newspaper notices as a parallel signal stream. |
| Delinquent taxpayer PDF | Medium | OCR / PDF text extraction (annual one-shot) |

---

## PROPOSED 6-PATTERN STACK (Mecklenburg / NC)

The framework spec's core principle is **orthogonal pattern categories**, not score inflation. Each pattern represents a distinct distress signal type. A property's tier is determined by **stack depth** (how many distinct categories fire), not raw cumulative score. A 3-pattern stack always beats a 1-pattern hit, no matter how many sub-flags fire inside one category.

The TX framework uses: `lp`, `fc`, `tax`, `jud`, `probate`, `lien`. NC needs different categories because the underlying legal regime differs. Below is the proposed Mecklenburg stack:

### Pattern 1 — `jfc` — Judicial Foreclosure (Power of Sale)

**What it fires on**
- New SP-numbered case in Clerk of Superior Court with case description matching `Foreclosure of Deed of Trust` / `Power of Sale`
- Notice of Substitute Trustee Sale recorded at Register of Deeds
- Public foreclosure-sale notice published in Mecklenburg Times

**Why it replaces TX `lp` + `fc`**
NC has no separate trustee-sale notice doc type at the recorder. The signal is the SP filing, the substitute-trustee instrument at ROD, and the statutory newspaper notice — all three reference the same event.

**Strength** Highest single signal. A property in active foreclosure is the textbook motivated seller.

---

### Pattern 2 — `tax` — Tax Distress (delinquency + In Rem foreclosure)

**What it fires on**
- Owner appears on the annual delinquent-taxpayer list
- Property listed on Mecklenburg In Rem foreclosure schedule (GS 105-375)
- Mortgage-style tax foreclosure case (GS 105-374) at Clerk of Superior Court

**Why** NC tax foreclosure flow differs from TX. In Rem is fast (~1 year from delinquency to sale) and Mecklenburg uses it heavily. Owner who lets taxes go to In Rem is unambiguously distressed.

**Strength** High. Two-stage signal — early (delinquent list) and late (In Rem filing).

---

### Pattern 3 — `estate` — Probate / Estate Opened

**What it fires on**
- New E-numbered estate file in Clerk of Superior Court within last 12 months
- Decedent name matches an owner of record on a Mecklenburg parcel (POLARIS join)
- Bonus sub-flag: heirs at multiple out-of-state addresses (heirs property indicator)

**Why** This is Quentin's specialty. NC heirs-property volume is significant and most TX-style frameworks under-weight it. Treat it as a top-tier category, not a long-tail bonus.

**Strength** High when joined to a parcel. Low when the estate has no real property — those filter out via the join.

---

### Pattern 4 — `code` — Code Violation / Demolition Order

**What it fires on**
- Open code-enforcement case (Charlotte) on the parcel
- Repeat violator (>1 case in trailing 24 months) — sub-flag
- Order to Demolish issued — strong sub-flag
- Any County Code Enforcement record (towns + unincorporated) when feed available

**Why** Code violations correlate strongly with absentee owners and properties under-maintained → likely candidates for sale.

**Strength** Medium standalone, very high when stacked with `tax` or `estate`.

---

### Pattern 5 — `lien` — Recorded Lien or Civil Judgment

**What it fires on**
- Mechanics lien recorded at Register of Deeds against the property
- Memorandum of Judgment recorded against the owner (becomes a lien on real property in NC)
- HOA assessment lien
- IRS / state tax lien (federal liens are recorded at ROD in NC)

**Why** These are the NC analog of the TX `lien` / `jud` categories rolled together — both create liens on real property under NC law and surface at the same source.

**Strength** Medium. Strong when amount is large or stacked with foreclosure.

---

### Pattern 6 — `transfer` — Distressed Conveyance Pattern

**What it fires on**
- Quitclaim deed recorded in last 24 months (often signals heirship workout, divorce, or partial interest)
- Deed in lieu of foreclosure
- Deed for nominal consideration ($1, $10, or "love and affection") between non-spouses
- New owner of record < 12 months on a property with any other distress signal firing

**Why** Captures the "something just changed" signal that the TX framework doesn't explicitly model. In NC this is especially load-bearing because heirship deeds and partial-interest transfers presage distress.

**Strength** Medium standalone. Functions primarily as a stack multiplier — combined with `estate` or `lien` it's a near-certain motivated seller.

---

### Stack scoring rules (per FRAMEWORK_SPEC §3)

- One function — `matches(record)` — drives both filter counts and table content. No two-truths drift.
- `stack_count` = number of distinct pattern categories that fire.
- Tier comes from `stack_count`, not raw score:
  - **Hot** — stack_count ≥ 3
  - **Warm** — stack_count == 2
  - **Active** — stack_count == 1
  - Sub-flags (e.g. "amount > $50k", "out-of-state mailing addr", "absentee owner") add raw score within a tier but never promote a record to a higher tier.
- This prevents the score-inflation spiral the framework spec calls out as anti-pattern #9.

---

## NEXT STEPS (Phase 2 — separate work)

1. Enumerate POLARIS REST endpoints with a real HTTP client; map the parcel/owner/address layer schema. This unblocks every join.
2. Pull Charlotte code enforcement open data (lowest friction — start here).
3. Pull Mecklenburg In Rem foreclosure list (highest signal density per record).
4. Build the Register of Deeds scraper carefully (Aumentum rate limits).
5. eCourts Portal scraper last — highest blocker risk; consider scraping `mecktimes.com` foreclosure notices as a parallel feed.
6. Then `pipeline/build_leads.py` joins everything to PIDs and applies the 6-pattern stack.

Phase 1 ends here. ⚡
