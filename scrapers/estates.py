"""Mecklenburg estate / probate scraper — mecktimes.com (probate index).

Per RECON.md, the eCourts Portal is CAPTCHA-gated, so the canonical "newly
filed estate" stream isn't directly automatable. NC statute (GS 28A-14-1)
requires every estate to publish a Notice to Creditors in a county newspaper —
Mecklenburg Times — for 4 consecutive weeks. So the newspaper notices are a
parallel, fully-public estate-discovery channel.

This scraper targets the same mecktimes endpoint as foreclosures.py with a
different indexgroup. It shares the Cloudflare-bot-management constraint: this
machine's TLS fingerprint is filtered, so the live scrape returns 403. When
that happens the script preserves any seed data already on disk and exits
cleanly. To hydrate from a renderer (Playwright, curl_cffi, residential proxy),
the parser logic in this file accepts raw HTML via stdin (--from-stdin).

Estate notices rarely carry a property address — the join to POLARIS happens
later, in the pipeline, by matching `decedent_name` against parcel owner names.
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

LISTING_URL = "https://mecktimes.com/public-notice/search-results?indexgroup=probate&pageindex={page}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_PATH = RAW_DIR / "estates.jsonl"
CKPT_PATH = RAW_DIR / "estates.checkpoint.json"

RE_BLOCK = re.compile(r'<div class="col-md-11 pnsr-block[^"]*">(.*?)(?=<div class="col-md-11 pnsr-block|<div id="search-results-pagination|</main)', re.S)
RE_DATE = re.compile(r'<span class="result-date">([^<]+)</span>')
RE_DETAIL_ID = re.compile(r'detail=(\d+)')
RE_HEADING = re.compile(r'<div class="notice-heading">([^<]+)</div>')
RE_SUMMARY = re.compile(r'<div class="notice-summary">(.*?)</div>', re.S)
RE_COUNTY = re.compile(r'<strong>County:</strong>\s*([^<]+)<')
RE_E_CASE = re.compile(r'\b(\d{2}\s*E\s*\d{3,7}(?:-\d{1,4})?)\b', re.I)
RE_ESTATE_OF = re.compile(r"(?:estate of|matter of the estate of|claims against)\s+([A-Z][A-Za-z .,'\-]{3,80}?)(?:,?\s+(?:deceased|late of|a/?k/?a))", re.I)


class CloudflareBlocked(Exception):
    pass


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
                raise CloudflareBlocked(
                    "mecktimes.com 403 Cloudflare bot management; see scrapers/README.md"
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
    m_id = RE_DETAIL_ID.search(block_html)
    if not m_id:
        return None
    detail_id = int(m_id.group(1))
    m_h = RE_HEADING.search(block_html)
    m_d = RE_DATE.search(block_html)
    m_s = RE_SUMMARY.search(block_html)
    m_c = RE_COUNTY.search(block_html)
    summary_raw = m_s.group(1) if m_s else ""
    summary = re.sub(r"<[^>]+>", " ", summary_raw)
    summary = html.unescape(re.sub(r"\s+", " ", summary)).strip()
    case_number = ""
    m_e = RE_E_CASE.search(summary)
    if m_e:
        case_number = re.sub(r"\s+", "", m_e.group(1)).upper()
    decedent = ""
    m_n = RE_ESTATE_OF.search(summary)
    if m_n:
        decedent = m_n.group(1).strip().rstrip(",.; ").upper()
    heading = html.unescape(m_h.group(1).strip()) if m_h else ""
    if not decedent and heading:
        # Heading is usually "Last, First" — flip to FIRST LAST for owner-match
        parts = [p.strip() for p in heading.split(",", 1)]
        if len(parts) == 2:
            decedent = (parts[1] + " " + parts[0]).strip().upper()
        else:
            decedent = heading.upper()
    return {
        "detail_id": detail_id,
        "detail_url": f"https://mecktimes.com/public-notice/search-detail?indexgroup=probate&detail={detail_id}",
        "posted_date": m_d.group(1).strip() if m_d else "",
        "heading": heading,
        "decedent_name": decedent,
        "county": m_c.group(1).strip() if m_c else "",
        "case_number": case_number,
        "summary": summary[:1500],
    }


def parse_html(html_text: str) -> list[dict]:
    out = []
    for m in RE_BLOCK.finditer(html_text):
        rec = parse_block(m.group(1))
        if rec:
            out.append(rec)
    return out


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


def write_records(records: list[dict], state: dict, seen: set[int]) -> int:
    n = 0
    with OUT_PATH.open("a", encoding="utf-8") as fh:
        for r in records:
            if r["detail_id"] in seen:
                continue
            r["_source"] = "mecktimes_probate"
            fh.write(json.dumps(r, default=str) + "\n")
            seen.add(r["detail_id"])
            state["highest_detail_id"] = max(state["highest_detail_id"], r["detail_id"])
            state["total_written"] = state.get("total_written", 0) + 1
            n += 1
    return n


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
    print(f"[i] out:  {OUT_PATH}")
    print(f"[i] seen: {len(seen):,} previously-written notices")

    if args.from_stdin:
        html_text = sys.stdin.read()
        recs = parse_html(html_text)
        n = write_records(recs, state, seen)
        save_checkpoint(state)
        print(f"[done] stdin: parsed={len(recs)} new={n}")
        return 0

    flag = install_signal_handler()
    page = 1
    written = 0
    consecutive_all_seen = 0
    target = args.limit if args.limit and args.limit > 0 else None
    while True:
        if flag["stop"] or page > args.max_pages:
            break
        url = LISTING_URL.format(page=page)
        try:
            html_text = _http_get(url)
        except CloudflareBlocked as e:
            print(f"\n[!] {e}", file=sys.stderr)
            print(f"[i] {len(seen):,} existing seed records preserved in {OUT_PATH.name}", file=sys.stderr)
            break
        except Exception as e:
            print(f"[err] page {page}: {e}", file=sys.stderr)
            break
        recs = parse_html(html_text)
        if not recs:
            print(f"[i] page {page}: 0 — done")
            break
        n = write_records(recs, state, seen)
        save_checkpoint(state)
        print(f"[+] page {page:>3}: {len(recs):>2} parsed  {n:>2} new  total={state['total_written']}")
        written += n
        if target is not None and written >= target:
            print(f"[i] hit --limit {target}")
            break
        if n == 0:
            consecutive_all_seen += 1
            if consecutive_all_seen >= 2:
                print("[i] 2 consecutive pages with all-seen — caught up")
                break
        else:
            consecutive_all_seen = 0
        page += 1
        time.sleep(args.delay)

    save_checkpoint(state)
    print(f"[done] this_run={written:,} total={state['total_written']:,} highest_id={state['highest_detail_id']}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mecklenburg estate notices (mecktimes.com).")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--from-stdin", action="store_true",
                   help="Parse HTML from stdin instead of fetching (for renderer-bypass workflow)")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
