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
  lien      HOA lien / civil judgment / mechanics lien / lis pendens
  transfer  Distressed conveyance — quitclaim, post-decedent sale, nominal price

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

# ROD per-doctype JSONL — all optional; pipeline runs fine when missing.
ROD_DOT_PATH = RAW_DIR / "rod_dot.jsonl"
ROD_ASGN_PATH = RAW_DIR / "rod_asgn.jsonl"
ROD_SUBTRUSTEE_PATH = RAW_DIR / "rod_sub_trustee.jsonl"
ROD_JUDGMENT_PATH = RAW_DIR / "rod_judgment.jsonl"
ROD_MECHANICS_LIEN_PATH = RAW_DIR / "rod_mechanics_lien.jsonl"
ROD_QUITCLAIM_PATH = RAW_DIR / "rod_quitclaim.jsonl"
ROD_LIS_PENDENS_PATH = RAW_DIR / "rod_lis_pendens.jsonl"
ROD_SATISFACTION_PATH = RAW_DIR / "rod_satisfaction.jsonl"

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
    """Build keys for fuzzy owner matching — used to INDEX POLARIS owners.

    We emit:
      - 3-word LAST FIRST MIDDLE (and FIRST MIDDLE LAST) for high-precision joins
      - 2-word LAST FIRST (and FIRST LAST) when total signal >= 8 chars
      - never single-word keys ("JOHN" matches thousands of parcels)
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
        # 3-word keys
        keys.append(f"{a} {b} {c}")
        keys.append(f"{c} {b} {a}")
        keys.append(f"{c} {a} {b}")
        # extra 2-word combos with the third token
        if len(a) + len(c) >= 8:
            keys.append(f"{a} {c}")
            keys.append(f"{c} {a}")
    return keys


def decedent_match_keys(decedent_normalized: str) -> list[str]:
    """Build the ordered list of keys to try when matching an estate notice's
    decedent name against POLARIS owner_keys. Tighter than owner_keys():

      - 3-word: LAST FIRST MIDDLE  (high precision; preferred when available)
      - 2-word: LAST FIRST or FIRST LAST, but only if last is NOT in the top-50
        common-surnames list AND len(last) >= 5

    Returns an empty list when the decedent name is too short to safely join.
    """
    parts = [p for p in decedent_normalized.split() if p]
    if len(parts) < 2:
        return []
    keys: list[str] = []
    if len(parts) >= 3:
        # decedent in either canonical form
        keys.append(" ".join(parts[:3]))
        keys.append(f"{parts[2]} {parts[1]} {parts[0]}")
        keys.append(f"{parts[-1]} {parts[0]} {parts[1]}")
    # Last/first 2-word fallback — only if surname carries enough signal.
    # When parts come in as [FIRST, MIDDLE, LAST] or [FIRST, LAST]:
    last = parts[-1]
    first = parts[0]
    if len(last) >= 5 and last not in COMMON_SURNAMES:
        keys.append(f"{first} {last}")
        keys.append(f"{last} {first}")
    return list(dict.fromkeys(keys))  # de-dupe, preserve order


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
                "nc_pin": (r.get("nc_pin") or "").strip(),
                "address": site_addr,
                "street_num": (r.get("streetnumber") or "").strip(),
                "street_name": (r.get("streetname") or "").strip(),
                "loc_city": (r.get("loccity") or "").strip(),
                "owner": owner,
                "owner1_first": (r.get("ownrfrstnme") or "").strip(),
                "owner1_last": (r.get("ownrlstnme") or "").strip(),
                "owner2": owner2,
                "owner2_first": (r.get("ownr2frstnme") or "").strip(),
                "owner2_last": (r.get("ownr2lstnme") or "").strip(),
                "mail_addr": mail_addr,
                "mail_addr2": (r.get("mailaddr2") or "").strip(),
                "mail_city": mail_city,
                "mail_state": mail_state,
                "mail_zip": mail_zip,
                "absentee": absentee,
                # Structure
                "year_built": r.get("yearbuilt"),
                "eff_year_built": r.get("effyearblt"),
                "building_type": (r.get("bldgtype") or "").strip(),
                "land_use": (r.get("landuse_description") or "").strip(),
                "land_use_code": (r.get("lusecode") or "").strip(),
                "grade": (r.get("grade") or "").strip(),
                "story_height": (r.get("storyheight") or "").strip(),
                "vacant_or_improved": (r.get("vacorimprov") or "").strip(),
                "construction_frame": (r.get("frame") or "").strip(),
                "exterior_wall": (r.get("extwall") or "").strip(),
                "foundation": (r.get("foundation") or "").strip(),
                "roof_cover": (r.get("roofcover") or "").strip(),
                "heat": (r.get("heat") or "").strip(),
                "heat_fuel": (r.get("heatfuel") or "").strip(),
                "heated_area": r.get("heatedarea"),
                "total_area": r.get("totalarea"),
                "base_area": r.get("basearea"),
                "finished_area": r.get("finarea"),
                "garage_finished": r.get("fingarage"),
                "garage_unfinished": r.get("unfingarag"),
                "bedrooms": r.get("bedrooms"),
                "fullbath": r.get("fullbath"),
                "halfbath": r.get("halfbath"),
                "three_qtr_bath": r.get("threequabath"),
                "fireplaces": r.get("fireplaces"),
                "residential_units": r.get("resunits"),
                "commercial_units": r.get("comunits"),
                # Land
                "gis_acres": r.get("gisacres"),
                "total_acres": r.get("totalac"),
                "legal_acres": r.get("legalacres"),
                # Value
                "total_market_value": r.get("totmarkval"),
                "total_value": r.get("totalvalue"),
                "land_value": r.get("totlandval"),
                "building_value": r.get("totalbldgval"),
                "yard_value": r.get("totalyardval"),
                # Sale
                "sale_price": r.get("saleprice"),
                "sale_date": r.get("saledate"),
                "deed_type": (r.get("typeofdeed") or "").strip(),
                "valid_sale": (r.get("validsale") or "").strip(),
                "nal_desc": (r.get("naldesc") or "").strip(),
                "deed_book": (r.get("deed_book") or "").strip(),
                "deed_page": (r.get("deed_page") or "").strip(),
                "grantor": (r.get("grantor") or "").strip(),
                # Ownership classification
                "owner_type": (r.get("ownertyped") or "").strip(),
                "exemption": (r.get("exemption") or "").strip() if r.get("exemption") else "",
                # Geography
                "neighborhood": (r.get("neighborhood") or "").strip(),
                "neighborhood_desc": (r.get("neighbordesc") or "").strip(),
                "tax_district": (r.get("taxmundist") or "").strip(),
                "tax_fire_district": (r.get("taxfiredist") or "").strip(),
                "tax_special_district": (r.get("taxspecdist") or "").strip(),
                "legal_desc": (r.get("legaldesc") or "").strip(),
                # POLARIS x/y is encoded as (lat, lon) in CAMA — keep both as strings.
                "x_coord": (r.get("xcoord") or "").strip(),
                "y_coord": (r.get("ycoord") or "").strip(),
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


def _rod_pid(row: dict) -> str:
    """ROD rows carry `pid` already-normalized by rod.py; tolerate variants."""
    pid = (row.get("pid") or row.get("parcel_id") or row.get("ParcelId") or "").strip()
    digits = re.sub(r"\D", "", pid)
    return digits.zfill(8) if 0 < len(digits) <= 8 else digits


def _attach_rod_lien(leads_get_lead, polaris_by_pid: dict, rows: list, source_label: str) -> tuple[int, int]:
    """ROD judgment / mechanics_lien / lis_pendens → `lien` pattern."""
    attached = unmatched = 0
    for r in rows:
        pid = _rod_pid(r)
        if not pid or pid not in polaris_by_pid:
            unmatched += 1
            continue
        lead = leads_get_lead(pid)
        lead["patterns"].add("lien")
        lead["signals"]["lien"].append({
            "source": source_label,
            "doc_type": r.get("doc_type") or source_label,
            "instrument_id": r.get("instrument_id"),
            "book": r.get("book"), "page": r.get("page"),
            "recorded_date": r.get("recorded_date"),
            "grantor": r.get("grantor"), "grantee": r.get("grantee"),
            "amount": r.get("amount"),
        })
        attached += 1
    return attached, unmatched


def _attach_rod_transfer(leads_get_lead, polaris_by_pid: dict, rows: list) -> tuple[int, int]:
    """ROD quitclaim → `transfer` pattern."""
    attached = unmatched = 0
    for r in rows:
        pid = _rod_pid(r)
        if not pid or pid not in polaris_by_pid:
            unmatched += 1
            continue
        lead = leads_get_lead(pid)
        lead["patterns"].add("transfer")
        lead["signals"]["transfer"].append({
            "source": "rod_quitclaim",
            "doc_type": r.get("doc_type") or "QUITCLAIM",
            "instrument_id": r.get("instrument_id"),
            "book": r.get("book"), "page": r.get("page"),
            "recorded_date": r.get("recorded_date"),
            "grantor": r.get("grantor"), "grantee": r.get("grantee"),
        })
        attached += 1
    return attached, unmatched


def _attach_rod_dot_or_subtrustee(leads_get_lead, polaris_by_pid: dict, rows: list,
                                   doc_label: str, sub_flag: str) -> tuple[int, int]:
    """ROD DOT / Sub Trustee — strengthens `jfc` and adds a sub-flag, but does
    NOT independently fire `jfc` (a DOT alone isn't distress; the foreclosure
    notice does that)."""
    attached = unmatched = 0
    for r in rows:
        pid = _rod_pid(r)
        if not pid or pid not in polaris_by_pid:
            unmatched += 1
            continue
        lead = leads_get_lead(pid)
        if doc_label == "sub_trustee":
            lead["patterns"].add("jfc")
        lead["signals"]["jfc"].append({
            "source": f"rod_{doc_label}",
            "doc_type": r.get("doc_type") or doc_label.upper(),
            "instrument_id": r.get("instrument_id"),
            "book": r.get("book"), "page": r.get("page"),
            "recorded_date": r.get("recorded_date"),
            "grantor": r.get("grantor"), "grantee": r.get("grantee"),
        })
        lead["flags"].append(sub_flag)
        attached += 1
    return attached, unmatched


def _polaris_transfer_signal(parcel: dict, estate_signals: list[dict]) -> tuple[bool, list[str]]:
    """Detect transfer-pattern signal from POLARIS data alone.

    Two sub-rules — either fires the pattern:
      A. Recent sale (within ~24mo) at nominal consideration (price < $1k OR
         price < 5% of total market value) — common in heirship deeds and
         deed-in-lieu-of-foreclosure.
      B. Estate notice posted within 18mo PRIOR to a parcel sale — i.e. the
         decedent died, an estate was opened, and the property sold within
         18 months. Order matters: a sale that predates the estate notice
         doesn't fit this pattern.
    """
    flags: list[str] = []
    sale_dt = _ms_to_dt(parcel.get("sale_date"))
    try:
        sp = float(parcel.get("sale_price")) if parcel.get("sale_price") is not None else None
    except (TypeError, ValueError):
        sp = None
    tv_raw = parcel.get("total_market_value") or parcel.get("total_value")
    try:
        tv = float(tv_raw) if tv_raw is not None else None
    except (TypeError, ValueError):
        tv = None
    now = datetime.now(timezone.utc)
    recent = bool(sale_dt) and (now - sale_dt).days <= 730
    fired = False

    # Rule A — recent + nominal
    if recent and sp is not None:
        if sp < 1000 or (tv and tv > 0 and sp / tv < 0.05):
            flags.append("nominal_consideration_recent_sale")
            fired = True

    # Rule B — estate posted BEFORE sale, within 18mo of it
    if sale_dt and estate_signals:
        for sig in estate_signals:
            est_dt = _parse_estate_posted(sig.get("posted") or sig.get("posted_date") or "")
            if not est_dt:
                continue
            delta_days = (sale_dt - est_dt).days
            if 0 <= delta_days <= 548:  # 18mo
                flags.append("post_estate_recent_sale")
                fired = True
                break

    return fired, flags


def join_signals(
    polaris_by_pid: dict,
    by_addr_key: dict,
    by_owner_key: dict,
    tax_rows: list,
    foreclosure_rows: list,
    estate_rows: list,
    code_cases: list,
    code_demos: list,
    rod_judgment: list,
    rod_mechanics: list,
    rod_lis_pendens: list,
    rod_quitclaim: list,
    rod_dot: list,
    rod_asgn: list,
    rod_subtrustee: list,
    rod_satisfaction: list,
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

    # estates — decedent-name match using the *tighter* matcher than
    # owner_keys: requires last+first+middle when available, or last+first
    # only when last has >=5 chars and isn't in the top-50 common surnames.
    est_attached = est_unmatched = est_skipped_unsafe = 0
    for r in estate_rows:
        if (r.get("county") or "").strip().lower() != "mecklenburg":
            continue
        decedent = r.get("decedent_name") or ""
        norm = normalize_owner(decedent)
        keys = decedent_match_keys(norm)
        if not keys:
            est_skipped_unsafe += 1
            continue
        candidate_pids: set[str] = set()
        for k in keys:
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
    log(f"[estates] attached={est_attached} unmatched={est_unmatched} "
        f"skipped_unsafe={est_skipped_unsafe} (notice too short / common-surname guard)")

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

    # ---- ROD signals ---------------------------------------------------
    # Each is no-op when the corresponding rod_*.jsonl file isn't present,
    # so the pipeline runs cleanly without any ROD data at all.
    a, u = _attach_rod_lien(get_lead, polaris_by_pid, rod_judgment, "judgment")
    log(f"[rod:judgment] attached={a} unmatched={u}")
    a, u = _attach_rod_lien(get_lead, polaris_by_pid, rod_mechanics, "mechanics_lien")
    log(f"[rod:mechanics_lien] attached={a} unmatched={u}")
    a, u = _attach_rod_lien(get_lead, polaris_by_pid, rod_lis_pendens, "lis_pendens")
    log(f"[rod:lis_pendens] attached={a} unmatched={u}")
    a, u = _attach_rod_transfer(get_lead, polaris_by_pid, rod_quitclaim)
    log(f"[rod:quitclaim] attached={a} unmatched={u}")
    a, u = _attach_rod_dot_or_subtrustee(get_lead, polaris_by_pid, rod_dot, "dot", "rod_dot_present")
    log(f"[rod:dot] attached={a} unmatched={u}")
    a, u = _attach_rod_dot_or_subtrustee(get_lead, polaris_by_pid, rod_asgn, "asgn", "rod_servicer_change")
    log(f"[rod:asgn] attached={a} unmatched={u}")
    a, u = _attach_rod_dot_or_subtrustee(get_lead, polaris_by_pid, rod_subtrustee, "sub_trustee", "rod_substitute_trustee")
    log(f"[rod:sub_trustee] attached={a} unmatched={u}")
    # Satisfactions net out closed liens — sub-flag only, no pattern.
    sat_attached = 0
    for r in rod_satisfaction:
        pid = _rod_pid(r)
        if not pid or pid not in polaris_by_pid:
            continue
        get_lead(pid)["flags"].append("rod_satisfaction_filed")
        sat_attached += 1
    log(f"[rod:satisfaction] attached={sat_attached}")

    # POLARIS-derived transfer signal: nominal-consideration recent sale OR
    # estate-notice-then-sale-within-18mo.
    poll_transfer = 0
    for pid, lead in list(leads.items()):
        parcel = polaris_by_pid.get(pid)
        if not parcel:
            continue
        fired, sub_flags = _polaris_transfer_signal(parcel, lead["signals"].get("estate") or [])
        if fired:
            lead["patterns"].add("transfer")
            lead["flags"].extend(sub_flags)
            lead["signals"]["transfer"].append({
                "source": "polaris_sale",
                "sale_date_ms": parcel.get("sale_date"),
                "sale_price": parcel.get("sale_price"),
                "total_market_value": parcel.get("total_market_value"),
            })
            poll_transfer += 1
    log(f"[polaris:transfer] fired={poll_transfer}")

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

# Top-50 US surnames by Census 2010. We refuse to match estate notices to
# parcels on last+first alone when last is in this list — too noisy.
COMMON_SURNAMES = {
    "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
    "DAVIS", "RODRIGUEZ", "MARTINEZ", "HERNANDEZ", "LOPEZ", "GONZALEZ",
    "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE", "JACKSON", "MARTIN",
    "LEE", "PEREZ", "THOMPSON", "WHITE", "HARRIS", "SANCHEZ", "CLARK",
    "RAMIREZ", "LEWIS", "ROBINSON", "WALKER", "YOUNG", "ALLEN", "KING",
    "WRIGHT", "SCOTT", "TORRES", "NGUYEN", "HILL", "FLORES", "GREEN",
    "ADAMS", "NELSON", "BAKER", "HALL", "RIVERA", "CAMPBELL", "MITCHELL",
    "CARTER", "ROBERTS",
}

# Substrings that, when present in an owner name, mark the owner as an entity
# (LLC / corp / trust / etc.) rather than an individual.
ENTITY_TOKENS = {
    "LLC", "L.L.C", "L L C", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "CO.", "COMPANY", "TRUST", "TRUSTEE", "LP", "LLP", "L.P", "L.L.P",
    "PLLC", "PA", "P.A", "ASSOCIATES", "ASSOCIATION", "ASSOC", "FOUNDATION",
    "LIMITED", "PARTNERS", "PARTNERSHIP", "ESTATE OF",
}

# Substrings inside an entity name that suggest a landlord / investor (vs. a
# bank, religious org, HOA, etc).
LANDLORD_TOKENS = {
    "RENTAL", "RENTALS", "PROPERTIES", "PROPERTY", "HOLDINGS", "INVESTMENTS",
    "REALTY", "REAL ESTATE", "HOMES", "RE LLC", "REI", "GROUP",
}

NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _ms_to_dt(ms) -> datetime | None:
    if ms is None or ms == "":
        return None
    try:
        v = int(float(ms))
    except (TypeError, ValueError):
        return None
    if not (0 < v < 4_102_444_800_000):
        return None
    try:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_estate_posted(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_entity_owner(owner: str) -> bool:
    if not owner:
        return False
    up = " " + owner.upper() + " "
    for tok in ENTITY_TOKENS:
        if " " + tok + " " in up or " " + tok + "." in up:
            return True
    return False


def is_landlord_entity(owner: str) -> bool:
    if not owner:
        return False
    up = owner.upper()
    return is_entity_owner(owner) and any(tok in up for tok in LANDLORD_TOKENS)


def parse_owner_name(first_field: str, last_field: str) -> dict:
    """POLARIS stores names in two fields; the first field often contains
    multiple components ("BRIAN K", "JOHN DAVID", "BRIAN K JR"). Parse into
    first/middle/suffix/last components for downstream skip-trace prep.
    Entity owners get is_entity=True and the components are left empty.
    """
    full_first = (first_field or "").strip()
    last = (last_field or "").strip()
    full_name = f"{full_first} {last}".strip()
    if is_entity_owner(full_name):
        return {
            "first_name": "", "middle_name": "", "last_name": "",
            "suffix": "", "is_entity": True, "full_name": full_name,
        }
    parts = [p for p in full_first.split() if p]
    suffix = ""
    if parts and parts[-1].rstrip(".").upper() in NAME_SUFFIXES:
        suffix = parts.pop().rstrip(".").upper()
    first_name = parts[0] if parts else ""
    middle = " ".join(parts[1:]) if len(parts) > 1 else ""
    # POLARIS sometimes puts a JR/SR in the last field; pull it out.
    last_tokens = last.split()
    if last_tokens and last_tokens[-1].rstrip(".").upper() in NAME_SUFFIXES:
        suffix = suffix or last_tokens.pop().rstrip(".").upper()
        last = " ".join(last_tokens)
    return {
        "first_name": first_name,
        "middle_name": middle,
        "last_name": last,
        "suffix": suffix,
        "is_entity": False,
        "full_name": full_name,
    }


def compute_derived(parcel: dict, lead: dict) -> dict:
    """Pure-data derived fields. No I/O, no global state — easy to test."""
    out: dict = {}
    sale_dt = _ms_to_dt(parcel.get("sale_date"))
    today = datetime.now(timezone.utc)
    sp = parcel.get("sale_price")
    try:
        sp = float(sp) if sp not in (None, "") else None
    except (TypeError, ValueError):
        sp = None
    tmv = parcel.get("total_market_value") or parcel.get("total_value")
    try:
        tmv = float(tmv) if tmv not in (None, "") else None
    except (TypeError, ValueError):
        tmv = None
    lv = parcel.get("land_value")
    try:
        lv = float(lv) if lv not in (None, "") else None
    except (TypeError, ValueError):
        lv = None

    # equity_pct (very rough — POLARIS doesn't carry mortgage balance, so we
    # use the gap between purchase price and current market value as a floor
    # estimate of paydown + appreciation. Real equity is unknown without the
    # current loan balance from ROD/lender.)
    if sp is not None and tmv is not None and tmv > 0 and sp > 0:
        eq = max(0.0, min(100.0, (1.0 - sp / tmv) * 100.0))
        out["estimated_equity_pct"] = round(eq, 1)
    else:
        out["estimated_equity_pct"] = None

    # years_owned
    if sale_dt:
        out["years_owned"] = round((today - sale_dt).days / 365.25, 1)
    else:
        out["years_owned"] = None

    # absentee_strict + is_likely_landlord
    out["is_absentee"] = bool(parcel.get("absentee"))
    owner = parcel.get("owner") or ""
    is_entity = is_entity_owner(owner)
    out["is_entity"] = is_entity
    if out["is_absentee"] and not is_entity:
        out["is_likely_landlord"] = True
    elif is_landlord_entity(owner):
        out["is_likely_landlord"] = True
    else:
        out["is_likely_landlord"] = False

    # Exemption flags. POLARIS `exemption` is free text; common values include
    # "HOMESTEAD", "ELDERLY", "DISABLED VETERAN", "DISABLED", combinations.
    ex = (parcel.get("exemption") or "").upper()
    out["is_homestead"] = "HOMESTEAD" in ex
    out["is_senior"] = "ELDERLY" in ex or "ELDER " in ex or "OVER 65" in ex
    out["is_disabled_veteran"] = "DISABLED VET" in ex or "VETERAN" in ex
    out["is_disabled"] = "DISABLED" in ex and "VET" not in ex

    # is_likely_inherited — not a hard signal, just a flag worth eyeballing
    yb = parcel.get("year_built") or 0
    structure_age = (today.year - int(yb)) if yb else 0
    yo = out["years_owned"] or 0
    out["is_likely_inherited"] = (
        yo >= 25 and structure_age >= 50 and not is_entity
        and not out["is_homestead"]
    )

    # lot_value_pct — high = land play (teardown candidate or land bank)
    if lv is not None and tmv is not None and tmv > 0:
        out["lot_value_pct"] = round(min(100.0, (lv / tmv) * 100.0), 1)
    else:
        out["lot_value_pct"] = None

    # distress_score — a softer ranking number for in-tier sorting. Tier
    # itself still comes only from stack_count.
    score = 0
    score += len(lead.get("patterns", set())) * 10
    score += min(20, len(lead.get("flags", [])))
    if out["is_absentee"]:
        score += 5
    if out["is_senior"]:
        score += 5
    if out.get("estimated_equity_pct") is not None and out["estimated_equity_pct"] >= 50:
        score += 5
    if (out["years_owned"] or 0) >= 20:
        score += 3
    out["distress_score"] = score

    return out


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
    derived = compute_derived(parcel, lead)
    parsed_owner = parse_owner_name(parcel.get("owner1_first", ""), parcel.get("owner1_last", ""))
    parsed_owner2 = parse_owner_name(parcel.get("owner2_first", ""), parcel.get("owner2_last", ""))
    polaris_url = f"https://polaris3g.mecklenburgcountync.gov/?taxid={pid}" if pid else ""
    return {
        # ---- Identity ----
        "pid": pid,
        "nc_pin": parcel.get("nc_pin") or "",
        "polaris_url": polaris_url,
        # ---- Site address ----
        "address": parcel.get("address") or "",
        "street_num": parcel.get("street_num") or "",
        "street_name": parcel.get("street_name") or "",
        "city": parcel.get("loc_city") or "",
        # ---- Owner ----
        "owner": parcel.get("owner") or "",
        "owner2": parcel.get("owner2") or "",
        "owner_parsed": parsed_owner,
        "owner2_parsed": parsed_owner2,
        "is_entity": derived["is_entity"],
        # ---- Mailing address (structured for skip-trace export) ----
        "mail_addr": parcel.get("mail_addr") or "",
        "mail_addr2": parcel.get("mail_addr2") or "",
        "mail_city": parcel.get("mail_city") or "",
        "mail_state": parcel.get("mail_state") or "",
        "mail_zip": parcel.get("mail_zip") or "",
        # ---- Structure ----
        "year_built": parcel.get("year_built"),
        "eff_year_built": parcel.get("eff_year_built"),
        "building_type": parcel.get("building_type") or "",
        "land_use": parcel.get("land_use") or "",
        "land_use_code": parcel.get("land_use_code") or "",
        "grade": parcel.get("grade") or "",
        "story_height": parcel.get("story_height") or "",
        "vacant_or_improved": parcel.get("vacant_or_improved") or "",
        "exterior_wall": parcel.get("exterior_wall") or "",
        "foundation": parcel.get("foundation") or "",
        "roof_cover": parcel.get("roof_cover") or "",
        "heat": parcel.get("heat") or "",
        "heat_fuel": parcel.get("heat_fuel") or "",
        "heated_area": parcel.get("heated_area"),
        "total_area": parcel.get("total_area"),
        "bedrooms": parcel.get("bedrooms"),
        "fullbath": parcel.get("fullbath"),
        "halfbath": parcel.get("halfbath"),
        "fireplaces": parcel.get("fireplaces"),
        "residential_units": parcel.get("residential_units"),
        # ---- Land ----
        "gis_acres": parcel.get("gis_acres"),
        "total_acres": parcel.get("total_acres"),
        "legal_acres": parcel.get("legal_acres"),
        # ---- Value ----
        "total_market_value": parcel.get("total_market_value"),
        "total_value": parcel.get("total_value"),
        "land_value": parcel.get("land_value"),
        "building_value": parcel.get("building_value"),
        # ---- Sale ----
        "sale_price": parcel.get("sale_price"),
        "sale_date": parcel.get("sale_date"),
        "deed_type": parcel.get("deed_type") or "",
        "valid_sale": parcel.get("valid_sale") or "",
        "deed_book": parcel.get("deed_book") or "",
        "deed_page": parcel.get("deed_page") or "",
        # ---- Geography ----
        "neighborhood": parcel.get("neighborhood_desc") or parcel.get("neighborhood") or "",
        "tax_district": parcel.get("tax_district") or "",
        "owner_type": parcel.get("owner_type") or "",
        "exemption": parcel.get("exemption") or "",
        # ---- Derived ----
        "estimated_equity_pct": derived["estimated_equity_pct"],
        "years_owned": derived["years_owned"],
        "is_absentee": derived["is_absentee"],
        "is_likely_landlord": derived["is_likely_landlord"],
        "is_homestead": derived["is_homestead"],
        "is_senior": derived["is_senior"],
        "is_disabled_veteran": derived["is_disabled_veteran"],
        "is_disabled": derived["is_disabled"],
        "is_likely_inherited": derived["is_likely_inherited"],
        "lot_value_pct": derived["lot_value_pct"],
        "distress_score": derived["distress_score"],
        # ---- Pattern + scoring (kept identical to prior schema for compat) ----
        "patterns": s["patterns"],
        "stack_count": s["stack_count"],
        "tier": s["tier"],
        "raw_score": s["raw_score"],
        "flags": s["flags"],
        "signals": _trim_signals(lead["signals"]),
        # legacy alias for the dashboard
        "absentee": derived["is_absentee"],
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


def _git_commit_hash() -> str:
    """Best-effort current git commit (short hash). Returns "" on any failure."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


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

    # High-confidence warm = warm tier with at least one of:
    # demolition_order, code_housing_case, or absentee owner. Used in
    # methodology + dashboard quality metric.
    high_conf_warm = sum(
        1 for r in records
        if r["tier"] == "warm" and (
            "demolition_order" in r["flags"]
            or "code_housing_case" in r["flags"]
            or r.get("is_absentee")
        )
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git_commit_hash(),
        "total": len(records),
        "tier_counts": dict(tier_counts),
        "pattern_counts": dict(pattern_counts),
        "high_confidence_warm": high_conf_warm,
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
    rod_judgment = load_jsonl(ROD_JUDGMENT_PATH)
    rod_mechanics = load_jsonl(ROD_MECHANICS_LIEN_PATH)
    rod_lis_pendens = load_jsonl(ROD_LIS_PENDENS_PATH)
    rod_quitclaim = load_jsonl(ROD_QUITCLAIM_PATH)
    rod_dot = load_jsonl(ROD_DOT_PATH)
    rod_asgn = load_jsonl(ROD_ASGN_PATH)
    rod_subtrustee = load_jsonl(ROD_SUBTRUSTEE_PATH)
    rod_satisfaction = load_jsonl(ROD_SATISFACTION_PATH)
    log(f"[load] tax={len(tax):,}  foreclosures={len(fc):,}  "
        f"estates={len(est):,}  code_cases={len(code_cases):,}  code_demos={len(code_demos):,}")
    log(f"[load:rod] judgment={len(rod_judgment)}  mechanics={len(rod_mechanics)}  "
        f"lis_pendens={len(rod_lis_pendens)}  quitclaim={len(rod_quitclaim)}  "
        f"dot={len(rod_dot)}  asgn={len(rod_asgn)}  sub_trustee={len(rod_subtrustee)}  "
        f"satisfaction={len(rod_satisfaction)}")

    leads = join_signals(
        polaris_by_pid, by_addr, by_owner,
        tax, fc, est, code_cases, code_demos,
        rod_judgment, rod_mechanics, rod_lis_pendens, rod_quitclaim,
        rod_dot, rod_asgn, rod_subtrustee, rod_satisfaction,
        log,
    )
    log(f"[join] {len(leads):,} parcels with at least one signal")

    if args.dry_run > 0:
        dry_run_distribution(leads, polaris_by_pid, args.dry_run)
        return 0

    write_output(leads, polaris_by_pid, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
