"""Mecklenburg tax foreclosure scraper.

Per RECON.md: NC tax foreclosure runs in two flavors —
  - Mortgage-style (GS 105-374) — judicial process at Clerk of Superior Court
  - In Rem (GS 105-375) — faster, county-led, Mecklenburg uses heavily

Mecklenburg County contracts both flows out to two outside law firms whose
listings are the most timely public source:

  1. Kania Law Firm — kanialawfirm.com (~862 active Mecklenburg cases)
       Backed by a WordPress ninja_tables plugin with a public AJAX endpoint.
  2. Ruff, Bond, Cobb, Wade & Bethune (RBCWB) — rbcwb.com (~22 active cases)
       Backed by a static HTML table.

The annual delinquent-taxpayer PDF (tax.mecknc.gov) is published only once a
year and is harder to parse — covered by the active-foreclosure firms above
for any owner who has progressed past basic delinquency.

Output: data/raw/tax_delinquent.jsonl, one row per case, each tagged with
`_source` ("kania" or "rbcwb") and a `pid_normalized` field (POLARIS-style
zero-padded 8-char PID stripped of dashes).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import html as html_lib
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_PATH = RAW_DIR / "tax_delinquent.jsonl"
CKPT_PATH = RAW_DIR / "tax_delinquent.checkpoint.json"

KANIA_AJAX = "https://kanialawfirm.com/wp-admin/admin-ajax.php"
KANIA_TABLE_ID = "213701"
KANIA_REFERER = "https://kanialawfirm.com/tax-foreclosures-mecklenburg-county/foreclosure-listings/"
RBCWB_URL = "https://www.rbcwb.com/tax-foreclosure-listings/"


def _http_get(url: str, headers: dict | None = None, timeout: int = 30, retries: int = 4) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed: {url} ({last_err})")


def normalize_pid(raw: str) -> str:
    """Convert NC tax PID formats (e.g. '108-201-04') to POLARIS canonical '10820104'."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(8) if 0 < len(digits) <= 8 else digits


RE_PARCEL_IN_PAREN = re.compile(r"\(parcel\s*([0-9]+)\)", re.I)
RE_TRAILING_PID = re.compile(r"<br\s*/?>\s*([0-9]{5,15})\s*$", re.I)
RE_DOLLARS = re.compile(r"\$[\d,]+(?:\.\d{2})?")
RE_BR = re.compile(r"<br\s*/?>", re.I)


def _strip_html(s: str) -> str:
    return html_lib.unescape(RE_TAGS.sub("", RE_BR.sub(" | ", s or ""))).strip()


def _parse_kania_addressparcel(raw: str) -> tuple[str, str, str, str]:
    """Returns (pid, street, city_state_zip, raw_clean)."""
    m_paren = RE_PARCEL_IN_PAREN.search(raw or "")
    pid = m_paren.group(1) if m_paren else ""
    if not pid:
        m_tail = RE_TRAILING_PID.search(raw or "")
        if m_tail:
            pid = m_tail.group(1)
    parts = [_strip_html(p) for p in RE_BR.split(raw or "")]
    parts = [p for p in parts if p and not p.isdigit()]
    if parts and parts[0].lower().startswith("(parcel"):
        parts[0] = re.sub(r"^\(parcel\s*[0-9]+\)\s*", "", parts[0], flags=re.I)
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    return pid, street, city, _strip_html(raw or "")


def fetch_kania() -> list[dict]:
    """Pull Kania Law Firm's Mecklenburg tax foreclosure list via WordPress AJAX."""
    params = urllib.parse.urlencode({
        "action": "wp_ajax_ninja_tables_public_action",
        "table_id": KANIA_TABLE_ID,
        "target_action": "get-all-data",
    })
    url = f"{KANIA_AJAX}?{params}"
    headers = {
        "Accept": "application/json,*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": KANIA_REFERER,
    }
    raw = _http_get(url, headers=headers, timeout=45)
    rows = json.loads(raw)
    out = []
    for r in rows:
        v = r.get("value", r) if isinstance(r, dict) else {}
        addrp = v.get("addressparcel", "")
        if not addrp or addrp.strip() in ("", "&nbsp;"):
            continue  # skip placeholder/empty rows
        pid, street, city, addr_clean = _parse_kania_addressparcel(addrp)
        st_tv = v.get("saletimetaxvalue", "")
        m_dollars = RE_DOLLARS.search(_strip_html(st_tv))
        tax_value = m_dollars.group(0) if m_dollars else ""
        sale_time = _strip_html(RE_DOLLARS.sub("", st_tv)).strip(" |")
        out.append({
            "_source": "kania",
            "address": addr_clean,
            "street": street,
            "city": city,
            "addressparcel_raw": addrp,
            "saledate": v.get("saledate", ""),
            "saletime": sale_time,
            "tax_value": tax_value,
            "openingbid": _strip_html(v.get("openingbid", "")),
            "currentbid": _strip_html(v.get("currentbid", "")),
            "specialprice": v.get("specialprice", ""),
            "closedate": v.get("closedate", ""),
            "courtfile": _strip_html(v.get("courtfile", "")),
            "ourfilenbr": _strip_html(v.get("ourfilenbr", "")),
            "county": v.get("county", "Mecklenburg"),
            "pid_normalized": normalize_pid(pid),
        })
    return out


RE_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
RE_TD = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
RE_TAGS = re.compile(r"<[^>]+>")


def _clean_cell(html_chunk: str) -> str:
    return html_lib.unescape(RE_TAGS.sub(" ", html_chunk)).strip()


def fetch_rbcwb() -> list[dict]:
    """Pull RBCWB's Mecklenburg tax foreclosure HTML table."""
    body = _http_get(RBCWB_URL, headers={"Accept": "text/html"}).decode("utf-8", "replace")
    # First <table> on the page is the listings table (per probe).
    ti = body.find("<table")
    te = body.find("</table>", ti) + len("</table>")
    if ti < 0 or te <= ti:
        return []
    table = body[ti:te]
    rows = []
    seen_header = False
    for m in RE_TR.finditer(table):
        cells = [_clean_cell(c) for c in RE_TD.findall(m.group(1))]
        cells = [c for c in cells if c]
        if not cells:
            continue
        if not seen_header:
            seen_header = True
            continue  # skip header row
        if len(cells) < 6:
            continue
        name, address, zipcode, parcel, courtfile, status = cells[:6]
        last_upset = cells[6] if len(cells) > 6 else ""
        rows.append({
            "_source": "rbcwb",
            "name": name,
            "address": address,
            "zip": zipcode,
            "addressparcel": parcel,
            "courtfile": courtfile,
            "status": status,
            "last_day_for_upset": last_upset,
            "county": "Mecklenburg",
            "pid_normalized": normalize_pid(parcel),
        })
    return rows


def load_checkpoint() -> dict:
    if not CKPT_PATH.exists():
        return {"started_at": None, "last_run_at": None, "kania_count": 0, "rbcwb_count": 0}
    return json.loads(CKPT_PATH.read_text(encoding="utf-8"))


def save_checkpoint(state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = CKPT_PATH.with_suffix(CKPT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(CKPT_PATH)


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if args.reset and OUT_PATH.exists():
        OUT_PATH.unlink()
        if CKPT_PATH.exists():
            CKPT_PATH.unlink()
        print(f"[reset] cleared previous output")

    state = load_checkpoint()
    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()

    # These are full-snapshot sources; rewrite output rather than append.
    rows = []
    if args.source in ("all", "kania"):
        print("[i] fetching Kania Law Firm...")
        try:
            kania = fetch_kania()
            print(f"    -> {len(kania):,} rows")
            rows.extend(kania)
            state["kania_count"] = len(kania)
        except Exception as e:
            print(f"    [err] kania fetch failed: {e}", file=sys.stderr)
    if args.source in ("all", "rbcwb"):
        print("[i] fetching RBCWB...")
        try:
            rbcwb = fetch_rbcwb()
            print(f"    -> {len(rbcwb):,} rows")
            rows.extend(rbcwb)
            state["rbcwb_count"] = len(rbcwb)
        except Exception as e:
            print(f"    [err] rbcwb fetch failed: {e}", file=sys.stderr)

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
        print(f"[i] capped at --limit {args.limit}")

    with OUT_PATH.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    save_checkpoint(state)
    print(f"[done] wrote {len(rows):,} rows to {OUT_PATH}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mecklenburg tax foreclosure (delinquent) scraper.")
    p.add_argument("--source", choices=["all", "kania", "rbcwb"], default="all")
    p.add_argument("--limit", type=int, default=0, help="Cap rows total (0 = unlimited)")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
