# mecklenburg-intel

Motivated seller intelligence pipeline for **Mecklenburg County, NC** (Charlotte).

> **Status: Phase 1 — recon and scaffold only.** No scrapers built yet. See `RECON.md` for source inventory and the proposed pattern stack.

---

## Scope

Build a flat-file pipeline that surfaces distressed-property leads in Mecklenburg County by joining:

- Parcel/assessor data (Mecklenburg GIS — POLARIS)
- Register of Deeds (deeds, deeds of trust, liens)
- Tax Collector (delinquencies, in rem foreclosures)
- Clerk of Superior Court (judicial foreclosures, special proceedings, estates)
- Charlotte/County code enforcement violations

Mecklenburg is **not** Texas. NC is a judicial-foreclosure state with different doc types, different terminology, and a centralized Tyler Odyssey eCourts Portal. The TX framework patterns transfer; the source plumbing does not. See `RECON.md` for the adapted 6-pattern stack.

---

## Layout

```
mecklenburg-intel/
├── pipeline/        # build_leads.py, enrichment, normalization (TBD)
├── scrapers/        # source-specific fetchers (TBD)
├── data/
│   └── raw/         # gitignored — raw scraped JSONL
├── index.html       # static dashboard (placeholder)
├── RECON.md         # data source inventory + 6-pattern stack
└── README.md
```

---

## Architecture (planned, not built)

```
Sources (scrapers fetch only)
        ↓
Normalizers (source format → canonical signal JSONL)
        ↓
pipeline/build_leads.py (joins, scores, ranks — single source of truth)
        ↓
data/leads.json
        ↓
index.html (filters/displays only — no business logic)
```

Defaults from the workspace constitution: static hosting, flat files, no DB, GitHub Pages deploy. See `C:\Dev\xcerebro-builds\CLAUDE.md`.

---

## Next phases

1. **Phase 2** — Scaffold scrapers for the highest-confidence sources first (POLARIS parcel pull, tax foreclosure list, code enforcement open data).
2. **Phase 3** — eCourts Portal + Register of Deeds. Both are anti-bot-heavy and need a careful approach.
3. **Phase 4** — `build_leads.py`, scoring, dashboard.
4. **Phase 5** — GitHub Actions automation + Pages deploy.

⚡ — Jarvis
