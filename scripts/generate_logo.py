"""Generate Aarva's podcast cover art.

Apple Podcasts requires 3000×3000 PNG, RGB, square. The design matches the
web renderer's brand: warm cream background, bold black "Aarva" wordmark,
saturated red accent dot, small editorial subtitle.

Run with the venv active:
    python scripts/generate_logo.py

Output: aarva/output/cover.png

Re-run any time the brand evolves. The publish script picks up the cover
automatically and the RSS feed references it via the feed_image config.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install pillow")
    sys.exit(1)


# ─── Brand tokens (mirror aarva/output/web_renderer.py) ──────────────────────
SIZE        = 3000
PAPER       = (245, 239, 224)   # warm cream
INK         = (10, 10, 10)
INK_MID     = (42, 42, 42)
ACCENT_RED  = (230, 57, 70)
ACCENT_NAVY = (29, 53, 87)

# ─── Font discovery ──────────────────────────────────────────────────────────
# macOS ships several Helvetica variants. We try the bold/black weights first
# (heavier weights look much better at this scale), falling back to whatever
# the system has.
FONT_CANDIDATES = [
    # (path, index, label)
    ("/System/Library/Fonts/Helvetica.ttc", 1, "Helvetica Bold"),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 4, "Helvetica Neue Bold"),
    ("/System/Library/Fonts/Supplemental/HelveticaNeue.ttc", 4, "Helvetica Neue Bold (Supplemental)"),
    ("/Library/Fonts/Helvetica.ttc", 1, "Helvetica Bold (Library)"),
    ("/System/Library/Fonts/Helvetica.ttc", 0, "Helvetica"),
]


def find_font() -> tuple[str, int, str]:
    for path, index, label in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                ImageFont.truetype(path, 100, index=index)
                return path, index, label
            except (OSError, IndexError):
                continue
    raise RuntimeError(
        "No suitable Helvetica variant found. Install Helvetica or modify "
        "FONT_CANDIDATES in this script."
    )


def main() -> None:
    font_path, font_index, font_label = find_font()
    print(f"Using font: {font_label}  ({font_path}, index={font_index})")

    # Wordmark sizing: target ~70% of canvas width, leaving 15% margin each side.
    # We iteratively pick a font size that fits — much more robust than guessing
    # against the textbbox, which can underestimate visual extent for some fonts.
    target_text_width = int(SIZE * 0.70)
    main_font_size = 1400
    while main_font_size > 200:
        candidate = ImageFont.truetype(font_path, main_font_size, index=font_index)
        bbox = ImageDraw.Draw(Image.new("RGB", (10, 10))).textbbox(
            (0, 0), "Aarva.", font=candidate
        )
        if bbox[2] - bbox[0] <= target_text_width:
            font_main = candidate
            break
        main_font_size -= 25
    else:
        font_main = ImageFont.truetype(font_path, 200, index=font_index)

    tag_font_size  = max(110, main_font_size // 7)
    font_tag = ImageFont.truetype(font_path, tag_font_size, index=font_index)
    # font_micro is built below after we auto-size it against the new
    # (longer) micro_text. Keeping the build at the use-site lets us iterate
    # without colliding with the tagline sizing.

    img  = Image.new("RGB", (SIZE, SIZE), PAPER)
    draw = ImageDraw.Draw(img)

    # ─── Wordmark: "Aarva." with red dot ──────────────────────────────────
    main_text = "Aarva"
    dot_text  = "."

    main_bbox = draw.textbbox((0, 0), main_text, font=font_main)
    dot_bbox  = draw.textbbox((0, 0), dot_text,  font=font_main)
    full_bbox = draw.textbbox((0, 0), main_text + dot_text, font=font_main)

    main_w  = main_bbox[2] - main_bbox[0]
    total_w = full_bbox[2] - full_bbox[0]
    total_h = full_bbox[3] - full_bbox[1]

    x_start = (SIZE - total_w) // 2 - full_bbox[0]
    y       = (SIZE - total_h) // 2 - full_bbox[1] - 120

    draw.text((x_start,          y), main_text, fill=INK,        font=font_main)
    draw.text((x_start + main_w, y), dot_text,  fill=ACCENT_RED, font=font_main)

    # ─── Tagline below ─────────────────────────────────────────────────────
    tagline = "the world as your classroom"
    tag_bbox = draw.textbbox((0, 0), tagline, font=font_tag)
    tag_w = tag_bbox[2] - tag_bbox[0]
    tag_x = (SIZE - tag_w) // 2 - tag_bbox[0]
    tag_y = y + total_h + 240
    draw.text((tag_x, tag_y), tagline, fill=INK_MID, font=font_tag)

    # ─── Bottom rule + label ──────────────────────────────────────────────
    rule_y = int(SIZE * 0.88)
    margin = int(SIZE * 0.08)
    draw.rectangle(
        [(margin, rule_y), (SIZE - margin, rule_y + 16)],
        fill=INK,
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
        candidate = ImageFont.truetype(font_path, micro_font_size, index=font_index)
        bbox = draw.textbbox((0, 0), micro_text, font=candidate)
        if bbox[2] - bbox[0] <= micro_target_width:
            font_micro = candidate
            break
        micro_font_size -= 5
    else:
        font_micro = ImageFont.truetype(font_path, 60, index=font_index)

    micro_bbox = draw.textbbox((0, 0), micro_text, font=font_micro)
    micro_w = micro_bbox[2] - micro_bbox[0]
    micro_x = (SIZE - micro_w) // 2 - micro_bbox[0]
    micro_y = rule_y + 60
    draw.text((micro_x, micro_y), micro_text, fill=INK, font=font_micro)

    # ─── Save ─────────────────────────────────────────────────────────────
    out_path = Path(__file__).resolve().parents[1] / "aarva" / "output" / "cover.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    print(f"Saved: {out_path}  ({SIZE}×{SIZE}, RGB)")


if __name__ == "__main__":
    main()
