"""Generate Aarva's podcast cover art.

Apple Podcasts requires 3000×3000 PNG, RGB, square. The design matches
the black+red web redesign (docs/session_plan_black_red_redesign.md):
near-black background, Anton uppercase "AARVA" wordmark in warm
off-white, a single red dot accent — the same "AARVA●" mark used by
the PWA icons (scripts/generate_pwa_icons.py), scaled up with a
tagline and editorial subtitle underneath.

Run with the venv active:
    python scripts/generate_logo.py

Output: aarva/output/cover.png

Re-run any time the brand evolves. The publish script picks up the cover
automatically and the RSS feed references it via the feed_image config.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install pillow")
    sys.exit(1)


# ─── Brand tokens (mirror base.html's Tailwind color tokens) ────────────────
SIZE          = 3000
BG_COLOUR     = (10, 10, 10)      # #0A0A0A night
TEXT_COLOUR   = (240, 229, 208)   # #F0E5D0 cream-text
MUTED_COLOUR  = (196, 186, 168)   # cream-light-ish, for the tagline/subtitle
ACCENT_COLOUR = (255, 42, 42)     # #FF2A2A red-accent

_REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = _REPO_ROOT / "scripts" / "fonts" / "Anton-Regular.ttf"

WORDMARK = "AARVA"
LETTER_SPACING_EM = 0.02
DOT_FRAC = 0.07  # dot diameter as a fraction of canvas width


def _wordmark_width(font: ImageFont.FreeTypeFont, spacing_px: float, dot_d: float) -> float:
    advances = [font.getlength(ch) for ch in WORDMARK]
    gaps = spacing_px * len(WORDMARK)
    return sum(advances) + gaps + dot_d


def main() -> None:
    if not FONT_PATH.exists():
        raise RuntimeError(
            f"Anton font not found at {FONT_PATH}. Download it from "
            "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
        )

    img  = Image.new("RGB", (SIZE, SIZE), BG_COLOUR)
    draw = ImageDraw.Draw(img)

    # ─── Wordmark: "AARVA" + red dot, same mark as the PWA icons ──────────
    dot_d = SIZE * DOT_FRAC
    target_w = SIZE * 0.70
    main_font_size = 1400
    while main_font_size > 100:
        candidate = ImageFont.truetype(str(FONT_PATH), main_font_size)
        spacing_px = main_font_size * LETTER_SPACING_EM
        if _wordmark_width(candidate, spacing_px, dot_d) <= target_w:
            font_main = candidate
            break
        main_font_size -= 20
    else:
        font_main = ImageFont.truetype(str(FONT_PATH), 100)
        spacing_px = 100 * LETTER_SPACING_EM

    total_w = _wordmark_width(font_main, spacing_px, dot_d)
    full_bbox = draw.textbbox((0, 0), WORDMARK, font=font_main)
    total_h = full_bbox[3] - full_bbox[1]

    x = (SIZE - total_w) / 2
    y = (SIZE - total_h) / 2 - full_bbox[1] - 120

    for ch in WORDMARK:
        bbox = draw.textbbox((0, 0), ch, font=font_main)
        draw.text((x - bbox[0], y), ch, fill=TEXT_COLOUR, font=font_main)
        x += font_main.getlength(ch) + spacing_px

    dot_cy = y + full_bbox[1] + total_h / 2
    draw.ellipse(
        [(x, dot_cy - dot_d / 2), (x + dot_d, dot_cy + dot_d / 2)],
        fill=ACCENT_COLOUR,
    )

    # ─── Tagline below ─────────────────────────────────────────────────────
    tag_font_size = max(110, main_font_size // 7)
    font_tag = ImageFont.truetype(str(FONT_PATH), tag_font_size)
    tagline = "the world as your classroom"
    tag_bbox = draw.textbbox((0, 0), tagline, font=font_tag)
    tag_w = tag_bbox[2] - tag_bbox[0]
    tag_x = (SIZE - tag_w) // 2 - tag_bbox[0]
    tag_y = int(y + total_h + 240)
    draw.text((tag_x, tag_y), tagline, fill=MUTED_COLOUR, font=font_tag)

    # ─── Bottom rule + label ──────────────────────────────────────────────
    # Cream, not red — the dot above is the one red accent (same "single
    # accent" rule the rest of the black+red redesign follows).
    rule_y = int(SIZE * 0.88)
    margin = int(SIZE * 0.08)
    draw.rectangle(
        [(margin, rule_y), (SIZE - margin, rule_y + 16)],
        fill=TEXT_COLOUR,
    )

    micro_text = "Carefully handpicked journalism, narrated - daily"

    # Auto-fit the micro text within the bottom rule. The rule spans
    # margin → SIZE-margin, so we target ~95% of that width to leave
    # breathing room. Start near the previous proportional default
    # (main_font_size // 10, ~110-140pt) and shrink in 5pt steps until
    # the rendered text fits. Floor of 60pt — below that it stops reading
    # cleanly at thumbnail sizes.
    micro_target_width = int((SIZE - 2 * margin) * 0.95)
    micro_font_size = max(70, main_font_size // 10)
    while micro_font_size > 60:
        candidate = ImageFont.truetype(str(FONT_PATH), micro_font_size)
        bbox = draw.textbbox((0, 0), micro_text, font=candidate)
        if bbox[2] - bbox[0] <= micro_target_width:
            font_micro = candidate
            break
        micro_font_size -= 5
    else:
        font_micro = ImageFont.truetype(str(FONT_PATH), 60)

    micro_bbox = draw.textbbox((0, 0), micro_text, font=font_micro)
    micro_w = micro_bbox[2] - micro_bbox[0]
    micro_x = (SIZE - micro_w) // 2 - micro_bbox[0]
    micro_y = rule_y + 60
    draw.text((micro_x, micro_y), micro_text, fill=TEXT_COLOUR, font=font_micro)

    # ─── Save ─────────────────────────────────────────────────────────────
    out_path = Path(__file__).resolve().parents[1] / "aarva" / "output" / "cover.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    print(f"Saved: {out_path}  ({SIZE}×{SIZE}, RGB)")


if __name__ == "__main__":
    main()
