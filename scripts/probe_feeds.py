"""Probe every enabled feed in publications.yaml — report what they actually return.

Run from the project root with the venv active:
    python scripts/probe_feeds.py

For each feed, prints:
  - HTTP status (or error class)
  - Total entries in the feed
  - Entries dated within the configured lookback window
  - The most recent entry's date (so you can see if the feed is stale)

Use this to diagnose ingestion: if Stage 1 produced zero articles from a
publication, this script tells you whether the feed itself is the problem
(unreachable, empty, all-stale) or whether the issue is downstream
(extraction, filtering, scoring).
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

try:
    import feedparser
    import requests
except ImportError as e:
    print(f"Missing dependency: {e.name}. Activate the venv first.")
    sys.exit(1)


# ─── Config ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
PUBS_PATH = ROOT / "aarva" / "config" / "publications.yaml"
PIPELINE_PATH = ROOT / "aarva" / "config" / "pipeline.yaml"

with PUBS_PATH.open() as f:
    pubs = yaml.safe_load(f)["publications"]

with PIPELINE_PATH.open() as f:
    pipeline_cfg = yaml.safe_load(f)
ingestion_cfg = pipeline_cfg.get("ingestion", {})
LOOKBACK_DAYS = ingestion_cfg.get("lookback_days", 7)
TIMEOUT = ingestion_cfg.get("http_timeout_seconds", 30)
UA = ingestion_cfg.get("user_agent", "Aarva/0.1 (research; +aarva.app)")

CUTOFF = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


def _entry_dt(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        v = entry.get(key)
        if v:
            return datetime(*v[:6], tzinfo=timezone.utc)
    return None


def probe(pub: dict) -> dict:
    name = pub["name"]
    url = pub["rss_url"]
    tier = pub["tier"]
    enabled = pub.get("enabled", True)
    out = {
        "tier": tier, "name": name, "url": url, "enabled": enabled,
        "status": "?", "http": None, "total": 0, "recent": 0,
        "newest": None, "error": None,
    }
    if not enabled:
        out["status"] = "disabled"
        return out
    try:
        # Mirror the runtime fetcher's headers (User-Agent + Accept
        # for RSS/Atom/XML). Some publishers do strict content-
        # negotiation and 406 when no Accept header is sent.
        r = requests.get(url, timeout=TIMEOUT, headers={
            "User-Agent": UA,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
            ),
        })
        out["http"] = r.status_code
        if r.status_code >= 400:
            out["status"] = f"HTTP {r.status_code}"
            return out
        feed = feedparser.parse(r.content)
        if feed.bozo and not feed.entries:
            out["status"] = "MALFORMED"
            out["error"] = str(feed.bozo_exception)[:80]
            return out
        out["total"] = len(feed.entries)
        newest = None
        for e in feed.entries:
            dt = _entry_dt(e)
            if dt:
                if newest is None or dt > newest:
                    newest = dt
                if dt >= CUTOFF:
                    out["recent"] += 1
        out["newest"] = newest
        out["status"] = "OK"
    except requests.exceptions.Timeout:
        out["status"] = "TIMEOUT"
    except requests.exceptions.SSLError as e:
        out["status"] = "SSL"
        out["error"] = str(e)[:80]
    except requests.exceptions.ConnectionError as e:
        out["status"] = "CONN"
        out["error"] = str(e)[:80]
    except Exception as e:
        out["status"] = type(e).__name__
        out["error"] = str(e)[:80]
    return out


def main() -> int:
    print(f"Probing {len(pubs)} feeds (lookback {LOOKBACK_DAYS} days)…")
    print()

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(probe, p): p for p in pubs}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: (r["tier"], r["name"]))

    print(f"{'Tier':<5} {'Publication':<24} {'Status':<10} {'HTTP':>5} "
          f"{'Total':>6} {'Recent':>7}  {'Newest':<11}  Notes")
    print("-" * 110)
    for r in results:
        newest = r["newest"].strftime("%Y-%m-%d") if r["newest"] else "—"
        notes = r["error"] or ""
        print(f"  {r['tier']:<3} {r['name']:<24} {r['status']:<10} "
              f"{str(r['http'] or '—'):>5} {r['total']:>6} {r['recent']:>7}  "
              f"{newest:<11}  {notes[:50]}")

    print()
    ok = [r for r in results if r["status"] == "OK"]
    fresh = [r for r in ok if r["recent"] > 0]
    stale = [r for r in ok if r["recent"] == 0]
    broken = [r for r in results if r["status"] not in ("OK", "disabled")]

    print(f"Summary:")
    print(f"  fresh (≥1 entry in last {LOOKBACK_DAYS} days):  "
          f"{len(fresh)}/{len(results)}")
    print(f"  reachable but no recent content:    {len(stale)}")
    print(f"  unreachable / errored:              {len(broken)}")
    if broken:
        print()
        print("Broken feeds (need URL fix or removal):")
        for r in broken:
            print(f"  [{r['tier']}] {r['name']:<24} {r['status']:<10}  {r['url']}")
            if r["error"]:
                print(f"      → {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
