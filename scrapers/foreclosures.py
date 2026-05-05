"""Mecklenburg foreclosure scraper — scrapes mecktimes.com legal notices.

Per RECON.md, the eCourts Portal (Tyler Odyssey) is CAPTCHA-gated and unreliable
for automated foreclosure-case discovery. NC statute requires foreclosure sales
to be advertised in a county newspaper — Mecklenburg Times — so the newspaper
notices are a parallel, fully-public signal stream.

Each notice contains:
  - SP case number (e.g., "25SP002187") — links back to eCourts if needed
  - Property address
  - Borrower name(s)
  - Deed of trust book / page reference
  - Sale (auction) date
  - Date the notice was posted

Source URL pattern:
  https://mecktimes.com/public-notice/search-results
      ?indexgroup=real_estate&pageindex=N

Resume model: pageindex cursor + detail_id dedup. We persist (a) the highest
detail_id ever seen and (b) the next pageindex to try. If we crawl from page 1
each run we'll re-see the same recent notices; the dedup set on detail_id stops
us from writing duplicates. Once we hit a page where every detail_id is already
seen, we stop (caught up).

Output: data/raw/foreclosures.jsonl, one notice per row.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LISTING_URL = "https://mecktimes.com/public-notice/search-results?indexgroup=real_estate&pageindex={page}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_PATH = RAW_DIR / "foreclosures.jsonl"
CKPT_PATH = RAW_DIR / "foreclosures.checkpoint.json"

# --- regex parsers ------------------------------------------------------
RE_BLOCK = re.compile(r'<div class="col-md-11 pnsr-block[^"]*">(.*?)(?=<div class="col-md-11 pnsr-block|<div id="search-results-pagination|</main)', re.S)
RE_DATE = re.compile(r'<span class="result-date">([^<]+)</span>')
RE_DETAIL_ID = re.compile(r'detail=(\d+)')
RE_HEADING = re.compile(r'<div class="notice-heading">([^<]+)</div>')
RE_SUMMARY = re.compile(r'<div class="notice-summary">(.*?)</div>', re.S)
RE_COUNTY = re.compile(r'<strong>County:</strong>\s*([^<]+)<')
RE_AUCTION = re.compile(r'<strong>Auction Date:</strong>\s*([^<]+)<')
RE_SP_CASE = re.compile(r'\b(\d{2}\s*SP\s*\d{3,7}(?:-\d{1,4})?)\b', re.I)
RE_DEED_BP = re.compile(r'BOOK\s+(\d{1,6})\s+(?:AT\s+)?PAGE\s+(\d{1,6})', re.I)
RE_TOTAL_RESULTS = re.compile(r'(\d[\d,]*)\s+results?', re.I)


class CloudflareBlocked(Exception):
    """Raised when Cloudflare blocks the request — see scrapers/README.md for the workaround."""


def _http_get(url: str, retries: int = 4, timeout: int = 60) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # Cloudflare bot management on mecktimes — TLS-fingerprint based, not UA based.
                raise CloudflareBlocked(
                    "mecktimes.com returned 403 (Cloudflare bot management). "
                    "Python urllib's TLS fingerprint is being filtered. "
                    "Workaround options: (1) curl_cffi with Chrome impersonation from a residential IP, "
                    "(2) Playwright with a real Chrome profile, (3) seed via WebFetch / external renderer "
                    "(see scrapers/README.md)."
                ) from e
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed: {url} ({last_err})")


def parse_block(block_html: str) -> dict | None:
    """Extract one notice's fields from a block of HTML."""
    m_id = RE_DETAIL_ID.search(block_html)
    if not m_id:
        return None
    detail_id = int(m_id.group(1))
    m_h = RE_HEADING.search(block_html)
    m_d = RE_DATE.search(block_html)
    m_s = RE_SUMMARY.search(block_html)
    m_c = RE_COUNTY.search(block_html)
    m_a = RE_AUCTION.search(block_html)
    summary_raw = (m_s.group(1) if m_s else "")
    summary_text = re.sub(r"<[^>]+>", " ", summary_raw)
    summary_text = html.unescape(re.sub(r"\s+", " ", summary_text)).strip()
    address = html.unescape(m_h.group(1).strip()) if m_h else ""
    sp_case = ""
    m_sp = RE_SP_CASE.search(summary_text)
    if m_sp:
        sp_case = re.sub(r"\s+", "", m_sp.group(1)).upper()
    deed_book = deed_page = ""
    m_bp = RE_DEED_BP.search(summary_text)
    if m_bp:
        deed_book, deed_page = m_bp.group(1), m_bp.group(2)
    return {
        "detail_id": detail_id,
        "detail_url": f"https://mecktimes.com/public-notice/search-detail?indexgroup=real_estate&detail={detail_id}",
        "posted_date": m_d.group(1).strip() if m_d else "",
        "address": address,
        "county": m_c.group(1).strip() if m_c else "",
        "auction_date": m_a.group(1).strip() if m_a else "",
        "sp_case_number": sp_case,
        "deed_book": deed_book,
        "deed_page": deed_page,
        "summary": summary_text[:1500],
    }


def parse_page(html_text: str) -> list[dict]:
    out = []
    for m in RE_BLOCK.finditer(html_text):
        rec = parse_block(m.group(1))
        if rec:
            out.append(rec)
    return out


def estimate_total_results(html_text: str) -> int | None:
    m = RE_TOTAL_RESULTS.search(html_text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def load_checkpoint() -> dict:
    if not CKPT_PATH.exists():
        return {"highest_detail_id": 0, "total_written": 0, "started_at": None, "last_run_at": None}
    return json.loads(CKPT_PATH.read_text(encoding="utf-8"))


def save_checkpoint(state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = CKPT_PATH.with_suffix(CKPT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(CKPT_PATH)


def load_seen_ids() -> set[int]:
    if not OUT_PATH.exists():
        return set()
    seen = set()
    with OUT_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                seen.add(int(json.loads(line).get("detail_id", 0)))
            except Exception:
                pass
    return seen


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            sys.exit(130)
        print("\n[!] interrupt — stopping after current page", file=sys.stderr)
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if args.reset:
        for p in (OUT_PATH, CKPT_PATH):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    state = load_checkpoint()
    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    seen = load_seen_ids()
    print(f"[i] out:    {OUT_PATH}")
    print(f"[i] seen:   {len(seen):,} previously-written notices")

    flag = install_signal_handler()
    page = 1
    written_this_run = 0
    consecutive_all_seen_pages = 0
    t0 = time.time()
    target = args.limit if args.limit and args.limit > 0 else None

    with OUT_PATH.open("a", encoding="utf-8") as fh:
        while True:
            if flag["stop"] or page > args.max_pages:
                break
            url = LISTING_URL.format(page=page)
            try:
                html_text = _http_get(url)
            except CloudflareBlocked as e:
                print(f"\n[!] {e}", file=sys.stderr)
                print(f"[i] {sum(1 for _ in OUT_PATH.open(encoding='utf-8')) if OUT_PATH.exists() else 0:,} "
                      f"existing seed records preserved in {OUT_PATH.name}", file=sys.stderr)
                break
            except Exception as e:
                print(f"[err] page {page}: {e}", file=sys.stderr)
                break
            if page == 1:
                est = estimate_total_results(html_text)
                if est is not None:
                    print(f"[i] estimated total real-estate notices: {est:,}")
            recs = parse_page(html_text)
            if not recs:
                print(f"[i] page {page}: 0 records — done")
                break
            new_count = 0
            for r in recs:
                if r["detail_id"] in seen:
                    continue
                fh.write(json.dumps(r, default=str) + "\n")
                seen.add(r["detail_id"])
                state["highest_detail_id"] = max(state["highest_detail_id"], r["detail_id"])
                state["total_written"] = state.get("total_written", 0) + 1
                written_this_run += 1
                new_count += 1
                if target is not None and written_this_run >= target:
                    break
            fh.flush()
            save_checkpoint(state)
            elapsed = time.time() - t0
            print(f"[+] page {page:>3}: {len(recs):>2} listed  {new_count:>2} new  "
                  f"this_run={written_this_run:>5}  total={state['total_written']:>6}  "
                  f"{elapsed:5.1f}s")
            if target is not None and written_this_run >= target:
                print(f"[i] hit --limit {target}")
                break
            if new_count == 0:
                consecutive_all_seen_pages += 1
                if consecutive_all_seen_pages >= 2:
                    print("[i] 2 consecutive pages with all-seen records — caught up")
                    break
            else:
                consecutive_all_seen_pages = 0
            page += 1
            time.sleep(args.delay)

    save_checkpoint(state)
    print(f"[done] this_run={written_this_run:,} total={state['total_written']:,} "
          f"highest_id={state['highest_detail_id']}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mecklenburg foreclosure notices (mecktimes.com).")
    p.add_argument("--limit", type=int, default=0, help="Cap notices fetched this run (0 = unlimited)")
    p.add_argument("--max-pages", type=int, default=200, help="Hard ceiling on pages to crawl")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between page requests")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
