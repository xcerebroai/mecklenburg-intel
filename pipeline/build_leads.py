"""build_leads.py — Mecklenburg lead pipeline.

Joins all data/raw/*.jsonl signal feeds against the POLARIS master parcel
table on PID (or, for sources without PIDs, on address or owner name) and
produces data/leads.json — the single artifact the dashboard consumes.

Scoring follows the FRAMEWORK_SPEC anti-inflation rule and the
RECON.md 6-pattern stack:

  jfc       Judicial foreclosure — SP case + sale notice
  tax       Tax distress — Kania/RBCWB filing or "Mecklenburg County v X"
  estate    Probate — newspaper Notice to Creditors → owner-name match
  code      Code violation / demolition order — open Charlotte case
  lien      HOA lien / civil judgment converted to real-property lien
  transfer  Distressed conveyance — not yet wired (needs ROD data)

Tier comes from STACK DEPTH (count of distinct patterns), not score sum.
This is non-negotiable per FRAMEWORK_SPEC §3 — score inflation is the
canonical anti-pattern.

  Hot     stack_count >= 3
  Warm    stack_count == 2
  Active  stack_count == 1
  (records with 0 patterns are not emitted)

Sub-flags add to raw_score for sorting *within* a tier but never promote.

The dashboard reuses the same `matches(filters, lead)` function this
pipeline emits flags for, ensuring filter counts and filter results
are derived from the same code path (no Two-Truths Bug).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_PATH = PROJECT_ROOT / "data" / "leads.json"

POLARIS_PATH = RAW_DIR / "polaris_taxparcel_camadata_layer0.jsonl"
TAX_PATH = RAW_DIR / "tax_delinquent.jsonl"
FORECLOSURE_PATH = RAW_DIR / "foreclosures.jsonl"
ESTATE_PATH = RAW_DIR / "estates.jsonl"
CODE_CASES_PATH = RAW_DIR / "code_violations_cases_all.jsonl"
CODE_DEMO_PATH = RAW_DIR / "code_violations_orders_to_demolish.jsonl"

PATTERNS = ["jfc", "tax", "estate", "code", "lien", "transfer"]
TIER_HOT = "hot"
TIER_WARM = "warm"
TIER_ACTIVE = "active"

# Code-case statuses that count toward the `code` pattern. Per real-data
# profile: 99% of Charlotte HNS cases are Closed; only Open+New (~3,957)
# represent live distress. The "high-signal" Housing case type captures the
# actual unsafe-structure cases (vs Nuisance which dominates by volume).
CODE_OPEN_STATUSES = {"Open", "New", "Active", "Pending"}
CODE_HIGH_SIGNAL_TYPES = {"Housing"}

# Suffix normalization for street-address matching.
STREET_SUFFIX = {
    "ST": "ST", "STREET": "ST",
    "AV": "AVE", "AVE": "AVE", "AVENUE": "AVE",
    "RD": "RD", "ROAD": "RD",
    "DR": "DR", "DRIVE": "DR",
    "LN": "LN", "LANE": "LN",
    "CT": "CT", "COURT": "CT",
    "CR": "CIR", "CIR": "CIR", "CIRCLE": "CIR",
    "PL": "PL", "PLACE": "PL",
    "BLVD": "BLVD", "BOULEVARD": "BLVD",
    "PKWY": "PKWY", "PARKWAY": "PKWY",
    "TER": "TER", "TERRACE": "TER",
    "WAY": "WAY", "WY": "WAY",
    "HWY": "HWY", "HIGHWAY": "HWY",
    "TRL": "TRL", "TRAIL": "TRL",
    "SQ": "SQ", "SQUARE": "SQ",
}

# Strip these from owner names before indexing/matching — entity noise that
# doesn't appear in newspaper notices.
NAME_NOISE = {
    "INC", "INCORPORATED", "LLC", "L L C", "LP", "L P", "PLLC",
    "CORP", "CORPORATION", "CO",
    "TRUST", "TRUSTEE", "REVOCABLE", "LIVING",
    "ETAL", "ET AL", "ET UX",
    "JR", "SR", "II", "III", "IV",
    "ESTATE", "DECEASED",
    "FAMILY", "REVOCABLE", "IRREVOCABLE",
    "FOUNDATION",
}

NUM_RE = re.compile(r"^\s*(\d+)")
NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")


def normalize_owner(name: str) -> str:
    if not name:
        return ""
    n = NON_ALNUM_RE.sub(" ", name.upper())
    parts = [p for p in n.split() if p and p not in NAME_NOISE]
    return " ".join(parts)


def owner_keys(normalized: str) -> list[str]:
    """Build keys for fuzzy owner matching.

    Single-word keys are unsafe — "JOHN" hits thousands of parcels. Require at
    least 2 words and at least 8 characters of total signal. Newspaper notices
    publish names "Last, First Middle"; POLARIS stores them in two fields
    (ownrlstnme + ownrfrstnme), so we emit both LAST_FIRST and FIRST_LAST.
    """
    parts = [p for p in normalized.split() if p]
    if len(parts) < 2:
        return []
    keys: list[str] = []
    a, b = parts[0], parts[1]
    if len(a) + len(b) >= 8:
        keys.append(f"{a} {b}")
        keys.append(f"{b} {a}")
    if len(parts) >= 3:
        c = parts[2]
        if len(a) + len(c) >= 8:
            keys.append(f"{a} {c}")
            keys.append(f"{c} {a}")
    return keys


def normalize_street(street_full: str) -> tuple[str, str]:
    """Returns (street_number, normalized_streetname).

    Strips trailing city/state/zip and unit suffixes. POLARIS addresses look
    like "9738 LOUGHLIN LN CHARLOTTE NC" with no commas; mecktimes addresses
    look like "9738 Loughlin Ln, Charlotte, NC, 28273". We canonicalize to
    "<num> <streetname> <suffix>" — everything after the first known street
    suffix gets truncated.
    """
    if not street_full:
        return ("", "")
    s = street_full.upper()
    s = re.sub(r",\s*\w[\w\s]*,?\s*NC[\s,0-9-]*$", "", s)
    s = re.sub(r"\s+\d{5}(-\d{4})?$", "", s)
    s = re.sub(r"\s+(UNIT|APT|STE|SUITE|#)\s*\S+", "", s)  # drop unit
    m = NUM_RE.match(s)
    if not m:
        return ("", "")
    num = m.group(1)
    rest = s[m.end():].strip()
    rest = NON_ALNUM_RE.sub(" ", rest)
    tokens = [t for t in rest.split() if t]
    # Truncate at the first known street suffix.
    cut = -1
    for i, t in enumerate(tokens):
        if t in STREET_SUFFIX:
            cut = i
            break
    if cut >= 0:
        tokens = tokens[: cut + 1]
        tokens[-1] = STREET_SUFFIX[tokens[-1]]
    return (num, " ".join(tokens))


def load_polaris(path: Path, log) -> tuple[dict, dict, dict]:
    """Returns (by_pid, by_addr_key, by_owner_key)."""
    by_pid: dict[str, dict] = {}
    by_addr_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_owner_key: dict[str, set[str]] = defaultdict(set)
    log(f"[polaris] reading {path.name}...")
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            pid = (r.get("pid") or "").strip()
            if not pid:
                continue
            # Multiple CAMA rows per PID exist; keep the first (highest objectid wins
            # because we read in order). For the POC this is fine.
            if pid in by_pid:
                continue
            owner = " ".join(filter(None, [
                (r.get("ownrfrstnme") or "").strip(),
                (r.get("ownrlstnme") or "").strip(),
            ])).strip()
            owner2 = " ".join(filter(None, [
                (r.get("ownr2frstnme") or "").strip(),
                (r.get("ownr2lstnme") or "").strip(),
            ])).strip()
            site_addr = (r.get("address") or "").strip()
            mail_addr = (r.get("mailaddr1") or "").strip()
            mail_city = (r.get("city") or "").strip()
            mail_state = (r.get("state") or "").strip()
            mail_zip = (r.get("zipcode") or "").strip()
            absentee = bool(mail_addr) and bool(site_addr) and (
                normalize_street(site_addr)[1] != normalize_street(mail_addr)[1]
            )
            entry = {
                "pid": pid,
                "address": site_addr,
                "street_num": (r.get("streetnumber") or "").strip(),
                "street_name": (r.get("streetname") or "").strip(),
                "loc_city": (r.get("loccity") or "").strip(),
                "owner": owner,
                "owner2": owner2,
                "mail_addr": mail_addr,
                "mail_city": mail_city,
                "mail_state": mail_state,
                "mail_zip": mail_zip,
                "absentee": absentee,
                "year_built": r.get("yearbuilt"),
                "total_market_value": r.get("totmarkval"),
                "total_value": r.get("totalvalue"),
                "land_value": r.get("totlandval"),
                "building_value": r.get("totalbldgval"),
                "heated_area": r.get("heatedarea"),
                "bedrooms": r.get("bedrooms"),
                "fullbath": r.get("fullbath"),
                "halfbath": r.get("halfbath"),
                "land_use": r.get("landuse_description"),
                "vacant_or_improved": r.get("vacorimprov"),
                "exemption": r.get("exemption"),
                "sale_price": r.get("saleprice"),
                "sale_date": r.get("saledate"),
                "deed_book": r.get("deed_book"),
                "deed_page": r.get("deed_page"),
                "owner_type": r.get("ownertyped"),
                "tax_district": r.get("taxmundist"),
            }
            by_pid[pid] = entry
            num, name = normalize_street(site_addr)
            if num and name:
                by_addr_key[(num, name)].append(pid)
            for nm in (owner, owner2):
                norm = normalize_owner(nm)
                for k in owner_keys(norm):
                    if len(k) >= 4:
                        by_owner_key[k].add(pid)
            n += 1
    log(f"[polaris] indexed {n:,} parcels  addr_keys={len(by_addr_key):,}  owner_keys={len(by_owner_key):,}")
    return by_pid, dict(by_addr_key), dict(by_owner_key)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def detect_jfc_or_tax(record: dict) -> str:
    """Distinguish judicial-foreclosure notices from tax-foreclosure notices.

    Mecktimes lumps both under indexgroup=real_estate. The distinguishing
    feature: tax foreclosures have 'MECKLENBURG COUNTY' as plaintiff, or are
    explicitly marked as TAX FORECLOSURE in the summary, or use CV case prefix
    (civil) instead of SP (special proceeding).
    """
    summary = (record.get("summary") or "").upper()
    case = (record.get("sp_case_number") or "").upper()
    if "TAX FORECLOSURE" in summary:
        return "tax"
    if "MECKLENBURG COUNTY" in summary and "PLAINTIFF" in summary:
        return "tax"
    if "MECKLENBURG COUNTY V" in summary or "MECKLENBURG COUNTY VS" in summary:
        return "tax"
    if "HOA" in summary or "HOMEOWNERS ASSOC" in summary or "HOMEOWNER'S ASSOC" in summary:
        return "lien"
    # CV cases for foreclosure are usually judicial sales not foreclosure-of-DOT,
    # but if there's an SP number too treat as jfc.
    if "SP" in case:
        return "jfc"
    if "CV" in case:
        # plain civil execution sales (e.g. Sheriff's executions for unpaid debt)
        return "lien"
    return "jfc"


def join_signals(
    polaris_by_pid: dict,
    by_addr_key: dict,
    by_owner_key: dict,
    tax_rows: list,
    foreclosure_rows: list,
    estate_rows: list,
    code_cases: list,
    code_demos: list,
    log,
) -> dict[str, dict]:
    """Walk all signal rows, attach to PIDs. Returns pid -> {patterns, signals}."""
    leads: dict[str, dict] = {}

    def get_lead(pid: str) -> dict:
        if pid not in leads:
            leads[pid] = {
                "pid": pid,
                "patterns": set(),
                "signals": {p: [] for p in PATTERNS},
                "flags": [],
            }
        return leads[pid]

    # tax_delinquent — direct PID join (when present), filter to Mecklenburg
    tax_attached = tax_unmatched = 0
    for r in tax_rows:
        if (r.get("county") or "").strip().lower() not in ("mecklenburg", ""):
            continue
        pid = (r.get("pid_normalized") or "").strip()
        if not pid or pid not in polaris_by_pid:
            tax_unmatched += 1
            continue
        lead = get_lead(pid)
        lead["patterns"].add("tax")
        lead["signals"]["tax"].append({
            "source": r.get("_source"),
            "courtfile": r.get("courtfile") or r.get("ourfilenbr"),
            "saledate": r.get("saledate"),
            "status": r.get("status"),
            "openingbid": r.get("openingbid"),
            "tax_value": r.get("tax_value"),
            "name": r.get("name"),
            "address": r.get("address") or r.get("street"),
        })
        tax_attached += 1
    log(f"[tax] attached={tax_attached}  unmatched={tax_unmatched} (no PID match)")

    # foreclosures — address-based join
    fc_attached = fc_unmatched = 0
    for r in foreclosure_rows:
        if (r.get("county") or "").strip().lower() != "mecklenburg":
            continue
        pattern = detect_jfc_or_tax(r)
        addr = r.get("address") or ""
        num, name = normalize_street(addr)
        candidates = by_addr_key.get((num, name), []) if num and name else []
        if not candidates:
            fc_unmatched += 1
            continue
        # If multiple parcels share the address (condo, etc.), attach to all.
        for pid in candidates:
            lead = get_lead(pid)
            lead["patterns"].add(pattern)
            lead["signals"][pattern].append({
                "source": "mecktimes",
                "case": r.get("sp_case_number"),
                "posted": r.get("posted_date"),
                "auction": r.get("auction_date"),
                "deed_book": r.get("deed_book"),
                "deed_page": r.get("deed_page"),
                "summary": (r.get("summary") or "")[:300],
                "url": r.get("detail_url"),
            })
            fc_attached += 1
    log(f"[foreclosures] attached={fc_attached} unmatched={fc_unmatched} (no address match)")

    # estates — owner-name fuzzy join
    est_attached = est_unmatched = 0
    for r in estate_rows:
        if (r.get("county") or "").strip().lower() != "mecklenburg":
            continue
        decedent = r.get("decedent_name") or ""
        norm = normalize_owner(decedent)
        candidate_pids: set[str] = set()
        for k in owner_keys(norm):
            if len(k) >= 4:
                candidate_pids |= by_owner_key.get(k, set())
        if not candidate_pids:
            est_unmatched += 1
            continue
        for pid in candidate_pids:
            lead = get_lead(pid)
            lead["patterns"].add("estate")
            lead["signals"]["estate"].append({
                "source": "mecktimes",
                "decedent": decedent,
                "posted": r.get("posted_date"),
                "case": r.get("case_number"),
                "url": r.get("detail_url"),
            })
            est_attached += 1
    log(f"[estates] attached={est_attached} unmatched={est_unmatched} (no owner match)")

    # code violations — direct PID join, only OPEN/NEW (closed cases are noise:
    # 99% of HNS rows are Closed and stretch back years).
    code_attached = code_unmatched = code_filtered_closed = 0
    pid_case_count: dict[str, int] = defaultdict(int)
    for r in code_cases:
        pid = (r.get("ParcelId") or "").strip().zfill(8)
        if not pid or pid not in polaris_by_pid:
            code_unmatched += 1
            continue
        if r.get("CaseStatus") not in CODE_OPEN_STATUSES:
            code_filtered_closed += 1
            continue
        lead = get_lead(pid)
        lead["patterns"].add("code")
        lead["signals"]["code"].append({
            "source": "charlotte_hns",
            "case": r.get("CaseNumber"),
            "type": r.get("CaseType"),
            "status": r.get("CaseStatus"),
            "address": r.get("FullAddress"),
            "created": r.get("DateCreated"),
            "closed": r.get("DateClosed"),
            "conclusion": r.get("Conclusion"),
        })
        if r.get("CaseType") in CODE_HIGH_SIGNAL_TYPES:
            lead["flags"].append("code_housing_case")
        pid_case_count[pid] += 1
        code_attached += 1
    for pid, ct in pid_case_count.items():
        if ct >= 2:
            leads[pid]["flags"].append("code_repeat_violator")
    log(f"[code:cases] attached={code_attached} closed_skipped={code_filtered_closed:,} "
        f"unmatched={code_unmatched}")

    # demolition orders — high-signal sub-flag on `code`
    demo_attached = 0
    for r in code_demos:
        pid = (r.get("ParcelId") or "").strip().zfill(8)
        if not pid or pid not in polaris_by_pid:
            continue
        lead = get_lead(pid)
        lead["patterns"].add("code")
        lead["signals"]["code"].append({
            "source": "charlotte_demo",
            "case": r.get("CaseNumber"),
            "status": r.get("CaseStatus"),
            "address": r.get("FullAddress"),
            "created": r.get("DateCreated"),
        })
        lead["flags"].append("demolition_order")
        demo_attached += 1
    log(f"[code:demos] attached={demo_attached}")

    return leads


def score_lead(lead: dict, parcel: dict) -> dict:
    """Compute tier (from stack depth) + raw_score (sub-flags only) + sub-flags."""
    patterns = sorted(lead["patterns"])
    stack_count = len(patterns)
    if stack_count >= 3:
        tier = TIER_HOT
    elif stack_count == 2:
        tier = TIER_WARM
    else:
        tier = TIER_ACTIVE

    # raw_score is purely for in-tier sorting. Tier is decided above.
    score = 0
    if "jfc" in patterns:
        score += 25
    if "tax" in patterns:
        score += 20
    if "estate" in patterns:
        score += 20
    if "code" in patterns:
        score += 12
    if "lien" in patterns:
        score += 12
    if "transfer" in patterns:
        score += 10

    # Sub-flag sub-scores (within-tier only — never promote tier)
    flags = list(lead["flags"])
    if parcel.get("absentee"):
        flags.append("absentee_owner")
        score += 5
    val = parcel.get("total_market_value") or parcel.get("total_value") or 0
    try:
        valf = float(val)
    except (TypeError, ValueError):
        valf = 0
    if valf >= 500_000:
        flags.append("value_over_500k")
        score += 4
    if "demolition_order" in flags:
        score += 8
    if (parcel.get("vacant_or_improved") or "").upper().startswith("V"):
        flags.append("vacant")
        score += 4
    if "estate" in patterns and (parcel.get("owner_type") or "").upper() in ("INDIVIDUAL", "I", ""):
        # Heirs-property indicator: estate-active + individual ownership
        flags.append("heirs_property_indicator")
        score += 6
    return {
        "patterns": patterns,
        "stack_count": stack_count,
        "tier": tier,
        "raw_score": score,
        "flags": sorted(set(flags)),
    }


SIGNAL_CAP_PER_PATTERN = 3


def _trim_signals(signals: dict) -> dict:
    """Keep at most SIGNAL_CAP_PER_PATTERN recent signals per pattern."""
    out = {}
    for k, lst in signals.items():
        if not lst:
            continue

        def _key(s):
            for f in ("posted", "auction", "saledate", "created", "closed"):
                v = s.get(f)
                if v:
                    return str(v)
            return ""
        ordered = sorted(lst, key=_key, reverse=True)[:SIGNAL_CAP_PER_PATTERN]
        out[k] = ordered
    return out


def build_lead_record(pid: str, parcel: dict, lead: dict) -> dict:
    s = score_lead(lead, parcel)
    return {
        "pid": pid,
        "address": parcel.get("address") or "",
        "city": parcel.get("loc_city") or "",
        "owner": parcel.get("owner") or "",
        "owner2": parcel.get("owner2") or "",
        "mail_addr": parcel.get("mail_addr") or "",
        "mail_city": parcel.get("mail_city") or "",
        "mail_state": parcel.get("mail_state") or "",
        "mail_zip": parcel.get("mail_zip") or "",
        "absentee": parcel.get("absentee", False),
        "year_built": parcel.get("year_built"),
        "total_market_value": parcel.get("total_market_value"),
        "land_use": parcel.get("land_use"),
        "vacant_or_improved": parcel.get("vacant_or_improved"),
        "owner_type": parcel.get("owner_type"),
        "patterns": s["patterns"],
        "stack_count": s["stack_count"],
        "tier": s["tier"],
        "raw_score": s["raw_score"],
        "flags": s["flags"],
        "signals": _trim_signals(lead["signals"]),
    }


def dry_run_distribution(leads: dict[str, dict], polaris_by_pid: dict, sample_n: int) -> None:
    """Print tier distribution from a sample, for sanity-checking before full output."""
    pids = list(leads.keys())[:sample_n]
    tiers = defaultdict(int)
    pat_counts = defaultdict(int)
    for pid in pids:
        rec = build_lead_record(pid, polaris_by_pid.get(pid, {}), leads[pid])
        tiers[rec["tier"]] += 1
        for p in rec["patterns"]:
            pat_counts[p] += 1
    print(f"\n=== DRY RUN: first {len(pids)} leads ===")
    for t in (TIER_HOT, TIER_WARM, TIER_ACTIVE):
        print(f"  {t:7s} {tiers[t]}")
    print(f"  pattern hits: {dict(pat_counts)}")


def write_output(leads: dict[str, dict], polaris_by_pid: dict, log) -> dict:
    records = []
    for pid, lead in leads.items():
        if not lead["patterns"]:
            continue
        rec = build_lead_record(pid, polaris_by_pid.get(pid, {}), lead)
        records.append(rec)

    # Sort: tier rank desc, then stack_count desc, then raw_score desc.
    tier_rank = {TIER_HOT: 3, TIER_WARM: 2, TIER_ACTIVE: 1}
    records.sort(key=lambda r: (tier_rank[r["tier"]], r["stack_count"], r["raw_score"]), reverse=True)

    tier_counts = defaultdict(int)
    pattern_counts = defaultdict(int)
    for r in records:
        tier_counts[r["tier"]] += 1
        for p in r["patterns"]:
            pattern_counts[p] += 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(records),
        "tier_counts": dict(tier_counts),
        "pattern_counts": dict(pattern_counts),
        "patterns_legend": {
            "jfc": "Judicial Foreclosure (Power of Sale)",
            "tax": "Tax Distress (delinquency / In Rem / mortgage-style)",
            "estate": "Probate / Estate Opened",
            "code": "Code Violation / Demolition Order",
            "lien": "Recorded Lien / Civil Judgment",
            "transfer": "Distressed Conveyance (not yet wired)",
        },
        "tier_rules": {
            "hot": "stack_count >= 3",
            "warm": "stack_count == 2",
            "active": "stack_count == 1",
        },
        "records": records,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, default=str), encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    log(f"\n[output] wrote {len(records):,} leads to {OUT_PATH} ({size_mb:.2f} MB)")
    log(f"[output] tier_counts:    {dict(tier_counts)}")
    log(f"[output] pattern_counts: {dict(pattern_counts)}")
    if size_mb > 50:
        log(f"[!] WARNING: leads.json exceeds 50MB GitHub cap. Trim sub-tier records or compress.")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", type=int, default=0,
                    help="If set, skip output and print tier distribution from N first leads.")
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    polaris_by_pid, by_addr, by_owner = load_polaris(POLARIS_PATH, log)
    tax = load_jsonl(TAX_PATH)
    fc = load_jsonl(FORECLOSURE_PATH)
    est = load_jsonl(ESTATE_PATH)
    code_cases = load_jsonl(CODE_CASES_PATH)
    code_demos = load_jsonl(CODE_DEMO_PATH)
    log(f"[load] tax={len(tax):,}  foreclosures={len(fc):,}  "
        f"estates={len(est):,}  code_cases={len(code_cases):,}  code_demos={len(code_demos):,}")

    leads = join_signals(polaris_by_pid, by_addr, by_owner, tax, fc, est, code_cases, code_demos, log)
    log(f"[join] {len(leads):,} parcels with at least one signal")

    if args.dry_run > 0:
        dry_run_distribution(leads, polaris_by_pid, args.dry_run)
        return 0

    write_output(leads, polaris_by_pid, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
