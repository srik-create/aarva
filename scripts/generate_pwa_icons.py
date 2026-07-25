"""Generate PWA + favicon icons for the Aarva web app.

Renders "AARVA" in Anton uppercase on the near-black brand background,
with a single red dot after the final "A" as the one red accent —
matching the black+red redesign (docs/session_plan_black_red_redesign.md).

Outputs (all in aarva/server/static/icons/):
  - icon-192.png            192x192  (PWA manifest)
  - icon-512.png            512x512  (PWA manifest + Media Session artwork)
  - icon-maskable-512.png   512x512  (Android adaptive-icon safe zone)
  - apple-touch-icon.png    180x180  (iOS home screen + lock-screen artwork)
  - favicon-32.png          32x32
  - favicon-16.png          16x16
  - favicon.ico             multi-size ICO (32 + 16)

Colours mirror the Tailwind tokens defined in base.html:
  background  #0A0A0A  (night)
  text        #F0E5D0  (cream-text)
  accent dot  #FF2A2A  (red-accent)

Re-run any time the wordmark or palette evolves:

    python scripts/generate_pwa_icons.py

Anton is bundled at scripts/fonts/Anton-Regular.ttf (pulled from the
same Google Fonts family the website loads) so this script doesn't
depend on system fonts.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install pillow")
    sys.exit(1)


_REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = _REPO_ROOT / "aarva" / "server" / "static" / "icons"
FONT_PATH = _REPO_ROOT / "scripts" / "fonts" / "Anton-Regular.ttf"

BG_COLOUR     = (10, 10, 10)      # #0A0A0A night
TEXT_COLOUR   = (240, 229, 208)   # #F0E5D0 cream-text
ACCENT_COLOUR = (255, 42, 42)     # #FF2A2A red-accent

WORDMARK = "AARVA"
# Matches the header masthead's tracking-wordmark (0.02em) — spacing is
# expressed as a fraction of font size since 1em == font size.
LETTER_SPACING_EM = 0.02
# Dot diameter as a fraction of icon width, per spec's "~6-8%".
DOT_FRAC = 0.07


def _wordmark_width(font: ImageFont.FreeTypeFont, spacing_px: float, dot_d: float) -> float:
    advances = [font.getlength(ch) for ch in WORDMARK]
    gaps = spacing_px * len(WORDMARK)  # one gap after each letter, incl. before the dot
    return sum(advances) + gaps + dot_d


def _fit_font(size: int, target_w: float, dot_d: float) -> tuple[ImageFont.FreeTypeFont, float]:
    """Shrink font size until AARVA + spacing + dot fits target_w."""
    font_size = size
    while font_size > 4:
        font = ImageFont.truetype(str(FONT_PATH), font_size)
        spacing_px = font_size * LETTER_SPACING_EM
        if _wordmark_width(font, spacing_px, dot_d) <= target_w:
            return font, spacing_px
        font_size -= 1
    font = ImageFont.truetype(str(FONT_PATH), 4)
    return font, 4 * LETTER_SPACING_EM


def make_icon(size: int, safe_zone_frac: float) -> Image.Image:
    """Render one icon. safe_zone_frac is the fraction of `size` the
    full wordmark+dot should occupy — smaller for maskable icons so
    Android's adaptive-icon crop can't clip the letters."""
    img = Image.new("RGB", (size, size), BG_COLOUR)
    draw = ImageDraw.Draw(img)

    dot_d = size * DOT_FRAC
    target_w = size * safe_zone_frac
    font, spacing_px = _fit_font(size, target_w, dot_d)

    total_w = _wordmark_width(font, spacing_px, dot_d)
    full_bbox = draw.textbbox((0, 0), WORDMARK, font=font)
    text_h = full_bbox[3] - full_bbox[1]

    x = (size - total_w) / 2
    y = (size - text_h) / 2 - full_bbox[1]

    for ch in WORDMARK:
        bbox = draw.textbbox((0, 0), ch, font=font)
        draw.text((x - bbox[0], y), ch, fill=TEXT_COLOUR, font=font)
        x += font.getlength(ch) + spacing_px

    dot_cy = y + full_bbox[1] + text_h / 2
    draw.ellipse(
        [(x, dot_cy - dot_d / 2), (x + dot_d, dot_cy + dot_d / 2)],
        fill=ACCENT_COLOUR,
    )
    return img


def main() -> None:
    if not FONT_PATH.exists():
        raise RuntimeError(
            f"Anton font not found at {FONT_PATH}. Download it from "
            "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # (filename, size, safe_zone_frac)
    targets = [
        ("icon-192.png", 192, 0.70),
        ("icon-512.png", 512, 0.70),
        ("apple-touch-icon.png", 180, 0.70),
        # Maskable: Android's adaptive-icon mask can crop up to ~33% of
        # each edge, so keep the wordmark within the centre ~50%.
        ("icon-maskable-512.png", 512, 0.50),
        ("favicon-32.png", 32, 0.70),
        ("favicon-16.png", 16, 0.70),
    ]

    icons: dict[str, Image.Image] = {}
    for filename, size, safe_zone_frac in targets:
        icon = make_icon(size, safe_zone_frac)
        icons[filename] = icon
        out = OUT_DIR / filename
        icon.save(out, "PNG", optimize=True)
        print(f"  Wrote: {out}  ({size}x{size})")

    ico_path = OUT_DIR / "favicon.ico"
    icons["favicon-32.png"].save(
        ico_path, format="ICO",
        sizes=[(32, 32), (16, 16)],
    )
    print(f"  Wrote: {ico_path}  (32x32 + 16x16)")


if __name__ == "__main__":
    main()
