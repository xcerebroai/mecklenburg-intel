"""Mecklenburg Register of Deeds scraper — CSV importer fallback.

Per RECON.md, ROD lives at meckrod.manatron.com on the Hart InterCivic /
Manatron Aumentum platform. **Live recon (Phase 3 night) found:**

  - The site is fully login-gated: the homepage IS a modal login form.
  - All search paths (/SearchByName, /SearchByDate, /SearchByDocType,
    /RealEstate*, /oncoredetails, /Search/SearchEntry) return a 238-byte
    "Session state is not available" stub if hit unauthenticated.
  - There is no documented public/guest credential. `guest`/`guest` and
    similar defaults fail the form validators.
  - The login form is ASP.NET WebForms with __VIEWSTATE (~9KB) and
    __EVENTVALIDATION tokens that change per session.
  - The platform is HartIC.WebUI behind IIS 10 — same product as Hart
    InterCivic's electronic recording. Production deployments at other NC
    counties offer free public accounts; Mecklenburg's deployment, as of
    Phase 3 reconnaissance, does not expose one publicly.

This puts ROD behind the user's stated stop condition for tonight's POC. The
file you're reading is **Approach 3** — the documented CSV-import path for
data sourced from a logged-in human session (or a future Playwright session
with persisted cookies).

## How to populate ROD data manually (until login is solved)

1. Log into https://meckrod.manatron.com from a browser with credentials.
2. Run a date-range search for a doc type (e.g., DEED OF TRUST, last 30 days).
3. Use the platform's CSV/Excel export feature, or manually copy the result
   table into a spreadsheet.
4. Save the file to data/raw/rod_<doctype>.csv (e.g., rod_dot.csv,
   rod_quitclaim.csv, rod_mechanics_lien.csv, rod_judgment.csv).
5. Run `python scrapers/rod.py --csv data/raw/rod_<doctype>.csv --doctype <doctype>`.

This script normalizes the import to the expected JSONL shape so
build_leads.py can join it just like any auto-scraped source.

## Expected CSV columns (case-insensitive, flexible)

  recorded_date, instrument_id (or doc_id), book, page,
  doc_type (free text — the script also takes --doctype), grantor,
  grantee, legal_description, parcel_id (8-digit canonical), amount, party

Any unknown columns are preserved verbatim under "extra".

## What you get

JSONL output at data/raw/rod_<doctype>.jsonl with one row per recorded
instrument. Each row has a normalized `pid` (when available), a normalized
`doc_type_code` (one of: dot, asgn, sub_trustee, judgment, mechanics_lien,
quitclaim, lis_pendens, satisfaction), and the original CSV columns under
`extra`.

The pipeline (build_leads.py) wires:
  - dot, sub_trustee  → reinforces `jfc` pattern when SP cases are nearby
  - asgn              → "servicer_change" sub-flag (pre-foreclosure indicator)
  - judgment, mechanics_lien, lis_pendens → `lien` pattern
  - quitclaim         → `transfer` pattern
  - satisfaction      → nets out closed liens (sub-flag only)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

# Map free-text doc-type strings (as they appear in Aumentum exports) to canonical codes.
DOCTYPE_CODES = {
    "dot": "dot", "deed of trust": "dot", "deed_of_trust": "dot",
    "asgn": "asgn", "assignment": "asgn", "assignment of dot": "asgn",
    "assignment of deed of trust": "asgn", "assn": "asgn",
    "sub_trustee": "sub_trustee", "substitute trustee": "sub_trustee",
    "notice of substitute trustee sale": "sub_trustee",
    "judgment": "judgment", "abstract of judgment": "judgment",
    "memo of judgment": "judgment", "memorandum of judgment": "judgment",
    "mechanics_lien": "mechanics_lien", "mechanics lien": "mechanics_lien",
    "claim of lien": "mechanics_lien",
    "qcd": "quitclaim", "quitclaim": "quitclaim", "quitclaim deed": "quitclaim",
    "quit claim deed": "quitclaim",
    "lis_pendens": "lis_pendens", "lis pendens": "lis_pendens",
    "notice of lis pendens": "lis_pendens",
    "satisfaction": "satisfaction", "release of lien": "satisfaction",
    "lien release": "satisfaction",
}

PID_RE = re.compile(r"\D")


def normalize_pid(raw: str) -> str:
    if not raw:
        return ""
    digits = PID_RE.sub("", raw)
    return digits.zfill(8) if 0 < len(digits) <= 8 else digits


def canonical_doctype(s: str) -> str:
    if not s:
        return ""
    key = s.strip().lower()
    if key in DOCTYPE_CODES:
        return DOCTYPE_CODES[key]
    for k, v in DOCTYPE_CODES.items():
        if k in key:
            return v
    return key.replace(" ", "_")


def detect_column(fieldnames: list[str], aliases: tuple[str, ...]) -> str | None:
    norm = {f.strip().lower().replace(" ", "_"): f for f in fieldnames}
    for a in aliases:
        if a in norm:
            return norm[a]
    return None


def import_csv(csv_path: Path, doctype_override: str | None) -> tuple[list[dict], dict]:
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        col_date = detect_column(cols, ("recorded_date", "date", "record_date", "filing_date"))
        col_inst = detect_column(cols, ("instrument_id", "doc_id", "instrument", "document"))
        col_book = detect_column(cols, ("book", "deed_book", "bk"))
        col_page = detect_column(cols, ("page", "deed_page", "pg"))
        col_doc = detect_column(cols, ("doc_type", "type", "instrument_type", "document_type"))
        col_grantor = detect_column(cols, ("grantor", "from", "from_party"))
        col_grantee = detect_column(cols, ("grantee", "to", "to_party"))
        col_legal = detect_column(cols, ("legal_description", "legal", "description"))
        col_pid = detect_column(cols, ("parcel_id", "pid", "parcel", "tax_id"))
        col_amount = detect_column(cols, ("amount", "consideration", "value"))
        for r in reader:
            doctype_raw = (r.get(col_doc) if col_doc else "") or doctype_override or ""
            extras = {k: v for k, v in r.items() if k not in {col_date, col_inst, col_book, col_page,
                                                              col_doc, col_grantor, col_grantee,
                                                              col_legal, col_pid, col_amount}}
            rows.append({
                "_source": "rod_csv_import",
                "doc_type": doctype_raw,
                "doc_type_code": canonical_doctype(doctype_raw or doctype_override or ""),
                "recorded_date": (r.get(col_date) or "").strip() if col_date else "",
                "instrument_id": (r.get(col_inst) or "").strip() if col_inst else "",
                "book": (r.get(col_book) or "").strip() if col_book else "",
                "page": (r.get(col_page) or "").strip() if col_page else "",
                "grantor": (r.get(col_grantor) or "").strip() if col_grantor else "",
                "grantee": (r.get(col_grantee) or "").strip() if col_grantee else "",
                "legal_description": (r.get(col_legal) or "").strip() if col_legal else "",
                "amount": (r.get(col_amount) or "").strip() if col_amount else "",
                "pid": normalize_pid((r.get(col_pid) or "")) if col_pid else "",
                "extra": extras,
            })
    summary = {"input": str(csv_path), "rows_in": len(rows), "doctype_override": doctype_override}
    return rows, summary


def write_jsonl(rows: list[dict], doctype_code: str) -> Path:
    out_path = RAW_DIR / f"rod_{doctype_code or 'unknown'}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    return out_path


def cmd_import(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[err] CSV not found: {csv_path}", file=sys.stderr)
        return 2
    rows, summary = import_csv(csv_path, args.doctype)
    if not rows:
        print(f"[!] no rows parsed from {csv_path}", file=sys.stderr)
        return 1
    # Group by doctype_code so a single CSV with mixed doc types splits properly.
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["doc_type_code"] or "unknown", []).append(r)
    written: list[Path] = []
    for code, lst in buckets.items():
        out = write_jsonl(lst, code)
        print(f"[+] {code:18s} {len(lst):>6} rows -> {out}")
        written.append(out)
    meta = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "outputs": [str(p) for p in written],
    }
    (RAW_DIR / "rod_import.log.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print("Mecklenburg ROD scraper — CSV import mode.")
    print()
    print("Live scraping is blocked: meckrod.manatron.com requires login and")
    print("does not expose a public guest credential. See the docstring at the")
    print("top of this file for the manual export workflow.")
    print()
    files = sorted(RAW_DIR.glob("rod_*.jsonl"))
    if not files:
        print("No rod_*.jsonl files found in data/raw/. The pipeline will run")
        print("without lien (judgment / mechanics) and transfer (quitclaim) signals")
        print("from this source — see scrapers/README.md for the gap.")
        return 0
    print(f"Existing ROD JSONL files in data/raw/:")
    for f in files:
        n = sum(1 for _ in f.open(encoding="utf-8"))
        print(f"  {f.name:35s} {n:,} rows")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mecklenburg ROD scraper — CSV importer (live scrape blocked).")
    sub = p.add_subparsers(dest="cmd")
    pi = sub.add_parser("import", help="Import a CSV exported from a logged-in Aumentum session.")
    pi.add_argument("--csv", required=True, help="Path to the exported CSV.")
    pi.add_argument("--doctype", default=None,
                    help="Doc type label to use for rows that don't carry one in the CSV.")
    pi.set_defaults(func=cmd_import)
    ps = sub.add_parser("status", help="Show what ROD data the pipeline currently sees.")
    ps.set_defaults(func=cmd_status)
    args = p.parse_args()
    if not args.cmd:
        args.cmd = "status"
        args.func = cmd_status
    return args


if __name__ == "__main__":
    args = parse_args()
    sys.exit(args.func(args))
