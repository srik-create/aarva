"""Diagnose a single article extraction end-to-end.

Run when retry_failed_extractions.py reports failures — this tells you which
layer is the problem (fetch / HTML structure / extraction algorithm).

Usage (venv active):
    python scripts/diagnose_extraction.py <URL>
    python scripts/diagnose_extraction.py https://aeon.co/essays/...

Prints:
  - HTTP status with each user-agent we try
  - Content-Type and response length
  - Whether the response looks like a paywall / interstitial / JS-only shell
  - What trafilatura extracts under each mode (precision, recall, baseline)
  - What a simple <article>/<main>/<body> raw-text extraction yields

The single most useful first signal: if the Aarva UA gets blocked but a Chrome
UA succeeds, the fix is to swap the user_agent in pipeline.yaml.
"""
from __future__ import annotations

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import trafilatura

AARVA_UA = "Aarva/0.1 (research; +aarva.app)"
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)


def looks_like_wall(html: str) -> str | None:
    """Heuristic: does this HTML look like a paywall / interstitial / JS shell?"""
    body_lower = html.lower()
    if "cf-error" in body_lower or "cloudflare" in body_lower and "challenge" in body_lower:
        return "Cloudflare challenge page"
    if "captcha" in body_lower and "verify" in body_lower:
        return "CAPTCHA wall"
    if 'name="robots" content="noindex' in body_lower:
        return "noindex page (often a wall)"
    # Aeon-style: real article HTML is ~100KB+. A shell page is 5-15KB.
    if len(html) < 15000 and "<script" in body_lower:
        return f"likely JS-rendered shell ({len(html)} bytes)"
    return None


def fetch(url: str, ua: str) -> tuple[int, str, dict[str, str]]:
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": ua}) as client:
        r = client.get(url)
        return r.status_code, r.text, dict(r.headers)


def extract_trafilatura(html: str, *, favor_recall: bool, no_fallback: bool = False) -> int:
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            favor_recall=favor_recall,
            no_fallback=no_fallback,
            output_format="txt",
        )
        return len((text or "").split())
    except Exception as e:
        print(f"      trafilatura raised {type(e).__name__}: {e}")
        return 0


def raw_body_words(html: str) -> int:
    """Sanity check: how much text is in the raw HTML (stripped of tags)?"""
    # Crude — strip scripts/styles, then drop tags.
    no_scripts = re.sub(r"<script[^>]*>.*?</script>", " ", html,
                        flags=re.DOTALL | re.IGNORECASE)
    no_styles = re.sub(r"<style[^>]*>.*?</style>", " ", no_scripts,
                       flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", no_styles)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text.split())


def main(url: str) -> int:
    print(f"Diagnosing: {url}")
    print()

    for label, ua in [("Aarva UA", AARVA_UA), ("Chrome UA", CHROME_UA),
                      ("Googlebot UA", GOOGLEBOT_UA)]:
        print(f"=== {label} ===")
        try:
            status, html, headers = fetch(url, ua)
        except Exception as e:
            print(f"  fetch failed: {type(e).__name__}: {e}")
            print()
            continue

        ct = headers.get("content-type", "?")
        size = len(html)
        print(f"  HTTP {status}  Content-Type: {ct}  Size: {size:,} bytes")

        wall = looks_like_wall(html)
        if wall:
            print(f"  ⚠ {wall}")

        raw_words = raw_body_words(html)
        print(f"  raw body text: {raw_words:,} words")

        if status == 200 and size > 1000:
            n_precision = extract_trafilatura(html, favor_recall=False)
            n_recall    = extract_trafilatura(html, favor_recall=True)
            n_baseline  = extract_trafilatura(html, favor_recall=True, no_fallback=True)
            print(f"  trafilatura precision : {n_precision:>5} words")
            print(f"  trafilatura recall    : {n_recall:>5} words")
            print(f"  trafilatura baseline  : {n_baseline:>5} words")

        print()

    print("Interpretation:")
    print("  - If raw body has thousands of words but trafilatura returns ~0,")
    print("    the page structure is unusual — needs site-specific rules.")
    print("  - If raw body is tiny and you see 'JS-rendered shell',")
    print("    the article is loaded by JavaScript; httpx can't see it.")
    print("    Options: switch to playwright, use the RSS summary, or skip.")
    print("  - If Aarva UA fails but Chrome UA succeeds, change user_agent")
    print("    in aarva/config/pipeline.yaml to the Chrome string above.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/diagnose_extraction.py <URL>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
