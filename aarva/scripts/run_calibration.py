"""Run Stage 4 against the v1 hand-labelled calibration set.

For each of the 32 pieces in docs/aarva_calibration_set_v1.md, fetch the
article text, run it through the Stage 4+5+6 prompt, and compare the LLM
verdict to the user's PASS/FAIL label. Print:
  - Per-piece comparison (expected vs. actual + rationale)
  - Per-piece disagreements with brief notes
  - Overall agreement rate

Target: >= 85% agreement before we lock the prompt. Disagreements feed
prompt iteration.

Usage:
    # Run against URLs listed in the calibration set:
    python -m aarva.scripts.run_calibration

    # Run against a custom calibration file:
    python -m aarva.scripts.run_calibration --calibration path/to/file.md
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
import yaml

from aarva.clients.llm import build_llm_client
from aarva.config import load_pipeline_config
from aarva.sources.article_extractor import extract_article
from aarva.stages.stage_4_5_6_score import _load_prompts, _build_user_prompt


@dataclass
class CalibrationItem:
    number: int
    title: str
    publication: str
    url: Optional[str]
    expected: str    # "PASS" or "FAIL"
    user_note: str   # the user's labelling reason


def _parse_calibration_md(path: Path) -> list[CalibrationItem]:
    """Parse the calibration markdown into structured items.

    Robust parser: chunks the file into numbered items, then searches each
    chunk for the user's PASS/FAIL signal. Handles the irregular formatting
    in the v1 set (unclosed bold, multi-line notes, items with no verdict).
    """
    text = path.read_text()

    # Find every numbered anchor. Two shapes used in the v1 calibration set:
    #   Section A:  "<N>. **<Publication: Title>** — by ..."
    #   Section B:  "### <N>. <descriptive title>"
    # We capture the number and the chunk that follows, then look for the
    # bold publication/title header inside the chunk.
    anchor_pattern = re.compile(
        r"^(?:###\s+)?(\d+)\.\s+",
        re.MULTILINE,
    )
    anchors = [(m.start(), int(m.group(1))) for m in anchor_pattern.finditer(text)]

    entries: list[CalibrationItem] = []
    seen_numbers: set[int] = set()
    for i, (pos, number) in enumerate(anchors):
        # Skip duplicate matches (an anchor can appear multiple times if a
        # number happens to start a sentence later in the doc).
        if number in seen_numbers:
            continue
        # Restrict to plausible calibration item numbers (1–50).
        if number < 1 or number > 50:
            continue
        seen_numbers.add(number)

        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(text)
        chunk = text[pos:end]

        # Header: the first **bold** segment in the chunk. For Section A this
        # follows immediately; for Section B it follows after the ### heading
        # line and a blank line.
        header_match = re.search(r"\*\*([^\n*]+?)\*\*", chunk)
        if not header_match:
            continue
        header = header_match.group(1).strip()
        if ":" in header:
            publication, title_part = header.split(":", 1)
            title = title_part.strip().strip('"').strip("'").strip()
        else:
            publication, title = "", header

        # URL: first http(s) found in the chunk.
        url_match = re.search(r"https?://[^\s)\]]+", chunk)
        url = url_match.group(0) if url_match else None

        # User verdict: look in the text AFTER the "_My expectation: ..._" marker
        # for the first standalone PASS or FAIL keyword. We deliberately scan
        # the whole post-expectation chunk so multi-line notes work.
        expected = "UNKNOWN"
        user_note = ""
        exp_match = re.search(r"_My expectation:[^_]+_", chunk)
        if exp_match:
            post = chunk[exp_match.end():].strip()
            # Take the first ~200 chars of the user's note for display.
            user_note = re.sub(r"\s+", " ", post[:200]).strip()

            # Search for PASS / FAIL as word boundary, case-insensitive, in the
            # first 300 chars after the expectation marker. That window catches
            # the user's verdict marker (typically the first bolded word) while
            # excluding text from the next item.
            window = post[:300]
            pass_at = re.search(r"\bpass(?:es)?\b", window, re.IGNORECASE)
            fail_at = re.search(r"\bfail(?:s)?\b", window, re.IGNORECASE)
            if pass_at and (not fail_at or pass_at.start() < fail_at.start()):
                expected = "PASS"
            elif fail_at:
                expected = "FAIL"

        entries.append(CalibrationItem(
            number=number,
            title=title,
            publication=publication.strip(),
            url=url,
            expected=expected,
            user_note=user_note,
        ))

    return entries


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--calibration",
    default="docs/aarva_calibration_set_v1.md",
    help="Path to the calibration markdown file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--limit", type=int, default=None,
    help="Only run the first N pieces (for quick smoke tests).",
)
@click.option(
    "--start", type=int, default=1,
    help="Start at item number N (default 1).",
)
def main(calibration: Path, limit: Optional[int], start: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    items = _parse_calibration_md(calibration)
    items = [i for i in items if i.expected in ("PASS", "FAIL")]
    items = [i for i in items if i.number >= start]
    if limit:
        items = items[:limit]

    print(f"Loaded {len(items)} calibration items with verdicts")
    print(f"From: {calibration}")
    print()

    config = load_pipeline_config()
    llm = build_llm_client(config.llm)
    prompts = _load_prompts()
    prompt_version = config.scoring.get("prompt_version", "v1")
    prompt_config = prompts["stage_4_5_6"][prompt_version]

    agreements = 0
    disagreements = []
    skipped = []

    for item in items:
        print(f"─── Item {item.number}: {item.title[:60]} ───")
        print(f"   Publication: {item.publication}")
        print(f"   Expected: {item.expected}   |   User note: {item.user_note[:60]}")

        if not item.url:
            print(f"   SKIPPED — no URL\n")
            skipped.append(item)
            continue

        extracted = extract_article(item.url)
        if not extracted or extracted.word_count < 200:
            print(f"   SKIPPED — extraction failed or too short "
                  f"({extracted.word_count if extracted else 0} words)\n")
            skipped.append(item)
            continue

        article = {
            "publication_name": item.publication,
            "published_date": "",
            "full_text": extracted.full_text,
        }
        try:
            prompt = _build_user_prompt(prompt_config, article)
            full_prompt = prompt_config.get("system", "") + "\n\n" + prompt
            response = llm.complete(full_prompt, expect_json=True)
            assert isinstance(response, dict)

            rigour = float(response.get("rigour") or 0)
            posture = float(response.get("posture") or 0)
            self_imp = float(response.get("self_implication") or 0)
            actual = (
                "PASS" if rigour >= 0.5 and posture >= 0.5 else "FAIL"
            )

            agreement = "✓ AGREE" if actual == item.expected else "✗ DISAGREE"
            if actual == item.expected:
                agreements += 1
            else:
                disagreements.append((item, actual, response))

            print(f"   Actual: {actual} (rigour={rigour:.2f}, posture={posture:.2f}, "
                  f"self={self_imp:.2f}) — {agreement}")
            print(f"     rigour:  {response.get('rigour_rationale', '')[:100]}")
            print(f"     posture: {response.get('posture_rationale', '')[:100]}")
        except Exception as e:
            print(f"   ERROR: {e}")
            skipped.append(item)
        print()

    # ─── Summary ───
    total = len(items) - len(skipped)
    print("=" * 70)
    print(f"SUMMARY: {agreements}/{total} agreement "
          f"({100*agreements/max(total,1):.1f}%), "
          f"{len(disagreements)} disagreements, {len(skipped)} skipped")
    print()

    if disagreements:
        print("Disagreements (worth iterating the prompt against):")
        for item, actual, _resp in disagreements:
            print(f"  • Item {item.number}: expected {item.expected}, got {actual}")
            print(f"    {item.title[:75]}")
            print(f"    User note: {item.user_note[:80]}")
        print()

    if skipped:
        print(f"Skipped {len(skipped)} items (no URL / extraction failed / error).")


if __name__ == "__main__":
    main()
