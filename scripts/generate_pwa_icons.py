"""Generate PWA icons for Aarva from the podcast cover art.

Reads `aarva/output/cover.png` (the 3000x3000 podcast cover produced by
`scripts/generate_logo.py`) and emits resized icons used by the web app
for "Add to Home Screen" / PWA installs:

  - aarva/server/static/icons/icon-192.png        (Android manifest)
  - aarva/server/static/icons/icon-512.png        (Android manifest)
  - aarva/server/static/icons/apple-touch-icon.png (iOS, 180x180)

Re-run any time the cover art changes:

    python scripts/generate_pwa_icons.py

This is intentionally a tiny resizer rather than a separate design — the
cover and the icons share the same mark so the app/web/podcast surfaces
stay visually consistent. When the proper Aarva logo lands (roadmap
deferred item #3), update `generate_logo.py` and re-run this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install pillow")
    sys.exit(1)


_REPO_ROOT = Path(__file__).resolve().parents[1]
COVER_PATH = _REPO_ROOT / "aarva" / "output" / "cover.png"
OUT_DIR = _REPO_ROOT / "aarva" / "server" / "static" / "icons"

# (filename, size_in_px) tuples for each icon variant we ship.
TARGETS = [
    ("icon-192.png", 192),
    ("icon-512.png", 512),
    ("apple-touch-icon.png", 180),
]


def main() -> None:
    if not COVER_PATH.exists():
        print(f"ERROR: {COVER_PATH} not found. Run scripts/generate_logo.py first.")
        sys.exit(1)

    cover = Image.open(COVER_PATH).convert("RGB")
    print(f"Source: {COVER_PATH}  ({cover.size[0]}x{cover.size[1]})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for filename, size in TARGETS:
        # LANCZOS gives crisp text at the smallest icon sizes.
        icon = cover.resize((size, size), Image.LANCZOS)
        out = OUT_DIR / filename
        icon.save(out, "PNG", optimize=True)
        print(f"  Wrote: {out}  ({size}x{size})")


if __name__ == "__main__":
    main()
