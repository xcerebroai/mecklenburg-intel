"""Charlotte code enforcement scraper.

Pulls two layers from the City of Charlotte's on-prem ArcGIS Server (HNS folder):

  1. CodeEnforcementCasesAll       — every code case ever filed (~431K)
  2. CodeEnforcementOrderstoDemolish — current demolition orders (~21, high signal)

Both layers carry `ParcelId` (zero-padded 8-char PID), which joins directly to
the POLARIS master parcel `pid`. Output is one JSONL file per layer in
data/raw/, each row tagged with `_source` so the pipeline can tell them apart.

Resume model: same objectid-cursor pattern as polaris.py — checkpoint persists
the last objectid per layer, restart picks up from `> last_oid`.

Coverage gap: this only covers the City of Charlotte. Mecklenburg County's
Code Enforcement (towns + unincorporated areas) is at code.mecknc.gov and has
no equivalent open-data feed identified yet — see scrapers/README.md.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LAYERS = {
    "cases_all": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementCasesAll/MapServer/0",
    "orders_to_demolish": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementOrderstoDemolish/MapServer/0",
}
PAGE_SIZE = 2000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"


def _http_get_json(url: str, retries: int = 4, timeout: int = 60) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last_err})")


def detect_oid_field(meta: dict) -> str:
    """Some Charlotte layers don't return objectIdField in metadata; find by type."""
    if meta.get("objectIdField"):
        return meta["objectIdField"]
    for f in meta.get("fields", []):
        if f.get("type") == "esriFieldTypeOID":
            return f["name"]
    return "OBJECTID"


def fetch_total_count(layer_url: str) -> int:
    d = _http_get_json(f"{layer_url}/query?where=1%3D1&returnCountOnly=true&f=json")
    return int(d.get("count", 0))


def fetch_page(layer_url: str, oid_field: str, last_oid: int, page_size: int) -> list[dict]:
    where = urllib.parse.quote(f"{oid_field} > {last_oid}")
    url = (
        f"{layer_url}/query"
        f"?where={where}"
        f"&outFields=*"
        f"&orderByFields={oid_field}+ASC"
        f"&resultRecordCount={page_size}"
        f"&returnGeometry=false"
        f"&f=json"
    )
    return _http_get_json(url).get("features", [])


def load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"last_objectid": 0, "total_fetched": 0, "started_at": None, "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            print("\n[!] second interrupt — exiting hard", file=sys.stderr)
            sys.exit(130)
        print("\n[!] interrupt — finishing current page then stopping...", file=sys.stderr)
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def scrape_layer(layer_key: str, layer_url: str, limit: int, page_size: int, reset: bool) -> dict:
    out_path = RAW_DIR / f"code_violations_{layer_key}.jsonl"
    ckpt_path = RAW_DIR / f"code_violations_{layer_key}.checkpoint.json"
    if reset:
        for p in (out_path, ckpt_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    print(f"\n=== {layer_key} ===")
    print(f"[i] url:    {layer_url}")
    meta = _http_get_json(f"{layer_url}?f=json")
    oid_field = detect_oid_field(meta)
    layer_max = int(meta.get("maxRecordCount") or PAGE_SIZE)
    page = min(page_size, layer_max)
    total = fetch_total_count(layer_url)
    print(f"[i] name:   {meta.get('name')}")
    print(f"[i] oid:    {oid_field}")
    print(f"[i] total:  {total:,}")
    print(f"[i] out:    {out_path}")

    state = load_checkpoint(ckpt_path)
    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    last_oid = int(state["last_objectid"])
    fetched = int(state["total_fetched"])
    if last_oid > 0:
        print(f"[i] resume: last_objectid={last_oid} already_fetched={fetched:,}")

    flag = install_signal_handler()
    pages = 0
    this_run = 0
    t0 = time.time()
    run_target = limit if limit and limit > 0 else None

    with out_path.open("a", encoding="utf-8") as fh:
        while True:
            if flag["stop"]:
                break
            features = fetch_page(layer_url, oid_field, last_oid, page)
            if not features:
                print("[i] empty page — done")
                break
            for feat in features:
                attrs = feat.get("attributes", {})
                oid = attrs.get(oid_field)
                if oid is None:
                    continue
                attrs["_source"] = layer_key
                fh.write(json.dumps(attrs, default=str) + "\n")
                if oid > last_oid:
                    last_oid = oid
                fetched += 1
                this_run += 1
                if run_target is not None and this_run >= run_target:
                    break
            fh.flush()
            pages += 1
            state["last_objectid"] = last_oid
            state["total_fetched"] = fetched
            save_checkpoint(ckpt_path, state)
            elapsed = time.time() - t0
            rate = this_run / elapsed if elapsed > 0 else 0
            print(f"[+] page {pages:>3}  oid<={last_oid:>10}  this_run={this_run:>7,}  "
                  f"total={fetched:>7,}/{total:,}  {rate:5.0f} rec/s")
            if run_target is not None and this_run >= run_target:
                print(f"[i] hit --limit {run_target}")
                break

    save_checkpoint(ckpt_path, state)
    return {"layer": layer_key, "fetched_total": fetched, "this_run": this_run, "last_oid": last_oid}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Charlotte code enforcement scraper.")
    p.add_argument("--layer", choices=list(LAYERS.keys()) + ["all"], default="all",
                   help="Which Charlotte HNS layer to scrape")
    p.add_argument("--limit", type=int, default=0, help="Cap rows per layer this run (0 = unlimited)")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    targets = LAYERS.keys() if args.layer == "all" else [args.layer]
    summary = []
    for k in targets:
        summary.append(scrape_layer(k, LAYERS[k], args.limit, args.page_size, args.reset))
    print("\n=== summary ===")
    for s in summary:
        print(f"  {s['layer']:25s} run={s['this_run']:>7,} total={s['fetched_total']:>7,} oid<={s['last_oid']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
