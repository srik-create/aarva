"""Re-classify the JTBD tag on already-scored articles.

Why this exists: when the JTBD selection logic was sharpened in
prompts.yaml (delight + smart_escape now take priority over the
curiosity catch-all), the change only affected NEW scoring runs. The
~1,400 already-scored articles in the DB still carry their old
biased tags — nearly all are tagged curiosity or keep_up_to_date,
with 0 delight and 4 smart_escape across all-time.

This script makes one focused Gemini call per article to re-classify
the JTBD fields ONLY (cheaper and faster than full Stage 4+5+6
re-scoring), and updates article_scores.jtbd_primary +
article_scores.jtbd_secondary in place.

Usage:
    python scripts/retag_jtbd.py --dry-run          # preview, no writes
    python scripts/retag_jtbd.py                    # apply
    python scripts/retag_jtbd.py --all              # also re-tag non-light pubs
    python scripts/retag_jtbd.py --limit 50         # cap (testing)

Targeting (default): articles where
  - article_scores.jtbd_primary IN ('curiosity', 'keep_up_to_date'),
  - article.status = 'scored',
  - article is NOT in any past edition_pieces row.

With --all, drops the JTBD-bucket filter — re-tags every scored
article regardless of current tag.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

# Allow running from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.clients.llm import build_llm_client, LLMResponseParseError
from aarva.config import load_pipeline_config
from aarva.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("retag_jtbd")


_JTBD_PROMPT = """\
You're re-classifying the Job-To-Be-Done (JTBD) for a previously-
scored article. The original tagging was biased toward the
"curiosity" catch-all; we're applying a sharpened priority order.

Return ONLY a JSON object with two fields. No preamble, no markdown.

JTBD options:
  - keep_up_to_date: main current-affairs happenings + deeper
    understanding (election, war, market move, major policy, ongoing
    political fight, breaking science). Most commentary lands here.
  - keep_ahead: pieces whose CENTRAL THESIS is that something is
    emerging or under-recognised — a new idea, trend, or development
    worth knowing about NOW because it'll matter LATER. Pick ONLY
    when trend-spotting is the spine of the piece. Not for current-
    affairs pieces that merely mention a trend.
  - curiosity: intellectually engaging but DOESN'T fit any other
    bucket. Heavy essays on ideas, history, philosophy, deep science
    explainers, durable cultural criticism. FALLBACK only.
  - smart_escape: entertaining, easy, constructive — the piece a
    reader picks up when they want to be engaged without being asked
    to work hard. About reader POSTURE more than subject. Includes
    light features ("The Americans Shelling Out for a Coat of Arms"),
    travel/food/place writing, profile pieces of interesting
    characters, cultural curios, slow living, art appreciation.
  - delight: LIGHT, FUN, PLAYFUL — humour, wit, oddities, surprising
    joys, viral curiosities, charming/quirky reporting, sports/games,
    wordplay. WRITING is genuinely light or funny — subject alone
    isn't enough. Narrower than smart_escape: delight is *fun*,
    smart_escape is *easy*.

SELECTION PRIORITY — pick the FIRST one that fits, in this order:
  1. delight (writing is genuinely light/fun/playful)
  2. smart_escape (engaging but doesn't ask reader to work hard)
  3. keep_ahead (CENTRAL thesis is "this is emerging")
  4. keep_up_to_date (current-affairs reporting — DEFAULT for politics
     and commentary)
  5. curiosity (fallback — intellectually engaging but doesn't fit
     any of the above)

Common mistakes to avoid:
  - Tagging an interesting historical piece as "curiosity" when the
    writing is light → that's "delight".
  - Tagging a light feature as "curiosity" when a tired reader would
    happily settle into it → that's "smart_escape".
  - Tagging a current-affairs piece as "keep_ahead" because it
    mentions a trend in passing — keep_ahead requires trend-spotting
    to be the CENTRAL thesis.

HOW TO USE THE FINGERPRINT:
  - cognitive_density 1-3 + voice "conversational"/"intimate"/"lyrical"
    → strong smart_escape signal (engaging but easy)
  - voice "playful" + emotional register skewed to joyful/comforting
    → strong delight signal
  - cognitive_density 5-7 + voice "analytical"/"authoritative"
    → likely curiosity or keep_up_to_date, NOT smart_escape
  - method "narrative_reporting"/"lived_experience"/"interview_dialogue"
    → often smart_escape
  - method "scholarly_analysis"/"data_driven" → almost never
    smart_escape (too demanding)

Article:
  Publication:  {pub}
  Title:        {title}
  Byline:       {byline}
  Word count:   {word_count}
  Fingerprint:  {fingerprint}

  Article body (this is the source of truth — judge from this):
  {article_body}

Return JSON:
{{
  "jtbd_primary":   "<one of the 5 options>",
  "jtbd_secondary": "<one of the 5 options, or null>"
}}
"""


_VALID_JTBD = {"keep_up_to_date", "keep_ahead", "curiosity",
               "smart_escape", "delight"}


def _load_candidates(
    db: Database,
    retag_all: bool,
    limit: int | None,
    pub_filter: str | None,
) -> list[dict]:
    """Load articles we want to consider for re-tagging.

    Also pulls the existing fingerprint_json + word_count so the retag
    prompt has access to voice_register / cognitive_density /
    emotional_register signals — these matter for distinguishing
    smart_escape ("engaging but easy") from curiosity ("intellectually
    engaging, asks the reader to work").
    """
    filter_clause = (
        ""
        if retag_all
        else "AND s.jtbd_primary IN ('curiosity', 'keep_up_to_date')"
    )
    pub_clause = (
        f"AND LOWER(p.name) LIKE '%{pub_filter.lower()}%'"
        if pub_filter else ""
    )
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT a.id, a.title, a.byline,
                   a.full_text, a.excerpt, a.word_count,
                   p.name AS publication,
                   s.jtbd_primary AS old_primary,
                   s.jtbd_secondary AS old_secondary,
                   s.fingerprint_json
              FROM articles a
              JOIN article_scores s ON s.article_id = a.id
              JOIN publications p ON p.id = a.publication_id
             WHERE a.status = 'scored'
               AND (
                     (a.full_text IS NOT NULL AND a.full_text != '')
                  OR (a.excerpt IS NOT NULL AND a.excerpt != '')
               )
               AND a.id NOT IN (
                     SELECT DISTINCT article_id FROM edition_pieces
               )
               {filter_clause}
               {pub_clause}
             ORDER BY a.id DESC
        """).fetchall()
    out = [dict(r) for r in rows]
    if limit:
        out = out[:limit]
    return out


def _format_fingerprint(fp_json: str | None) -> str:
    """Extract the high-signal fingerprint fields and format them for
    the retag prompt. The fingerprint already captures register and
    cognitive load — exactly what distinguishes smart_escape (easy)
    from curiosity (asks the reader to work). Without it, the model
    is guessing from title + excerpt alone."""
    if not fp_json:
        return "(no fingerprint available)"
    import json
    try:
        fp = json.loads(fp_json)
    except (json.JSONDecodeError, TypeError):
        return "(fingerprint unparseable)"

    bits = []
    voice = fp.get("voice_register") or {}
    if isinstance(voice, dict):
        v_primary = voice.get("primary") or "—"
        v_secondary = voice.get("secondary")
        bits.append(f"voice={v_primary}" + (f"/{v_secondary}" if v_secondary else ""))
    cog = fp.get("cognitive_density")
    if cog is not None:
        bits.append(f"cognitive_density={cog}/7  (1=accessible, 7=demanding)")
    struct = fp.get("structural_form")
    if struct:
        bits.append(f"structural_form={struct}")
    method = fp.get("method_of_inquiry")
    if method:
        bits.append(f"method={method}")
    emo = fp.get("emotional_register") or {}
    if isinstance(emo, dict) and emo:
        # Top 2 emotional registers by weight
        sorted_emo = sorted(emo.items(), key=lambda kv: -float(kv[1] or 0))[:2]
        emo_str = ", ".join(f"{k}={v:.2f}" for k, v in sorted_emo if float(v or 0) > 0)
        if emo_str:
            bits.append(f"emotional={emo_str}")
    return "  ".join(bits) if bits else "(empty fingerprint)"


def _classify(llm, article: dict) -> dict | None:
    fingerprint_summary = _format_fingerprint(article.get("fingerprint_json"))
    # Prefer full_text; fall back to excerpt only if full text is
    # missing. We cap at 25,000 chars (~5000 words) to stay well within
    # Gemini Flash's input context while keeping the LLM cost bounded.
    # Stage 4-5-6 sends it uncapped; the cap here is only a safety net
    # for the rare ultra-long piece.
    body = article.get("full_text") or article.get("excerpt") or ""
    body = body[:25_000]
    prompt = _JTBD_PROMPT.format(
        pub=article["publication"] or "Unknown",
        title=article["title"] or "Untitled",
        byline=article["byline"] or "Unknown",
        article_body=body,
        word_count=article.get("word_count") or 0,
        fingerprint=fingerprint_summary,
    )
    try:
        result = llm.complete(prompt, expect_json=True)
    except LLMResponseParseError as e:
        logger.warning("article %d: parse failed — %s",
                       article["id"], str(e)[:120])
        return None
    except Exception as e:
        logger.warning("article %d: LLM call failed — %s",
                       article["id"], str(e)[:120])
        return None

    if not isinstance(result, dict):
        return None

    primary = (result.get("jtbd_primary") or "").strip().lower()
    secondary = result.get("jtbd_secondary")
    if isinstance(secondary, str):
        secondary = secondary.strip().lower()
        if secondary in ("null", "none", ""):
            secondary = None

    if primary not in _VALID_JTBD:
        logger.warning("article %d: returned invalid primary '%s'",
                       article["id"], primary)
        return None
    if secondary is not None and secondary not in _VALID_JTBD:
        logger.warning("article %d: returned invalid secondary '%s' — dropping",
                       article["id"], secondary)
        secondary = None

    return {"jtbd_primary": primary, "jtbd_secondary": secondary}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview re-tags without writing to the DB.")
    ap.add_argument("--all", action="store_true",
                    help="Re-tag every scored article, not just those currently "
                         "tagged 'curiosity' or 'keep_up_to_date'.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of articles processed.")
    ap.add_argument("--pub", type=str, default=None,
                    help="Only consider articles from publications matching "
                         "this substring (case-insensitive). Useful for "
                         "testing on light pubs, e.g. --pub smithsonian.")
    args = ap.parse_args()

    config = load_pipeline_config()
    db = Database(config.db_path)
    llm = build_llm_client(config.llm)

    candidates = _load_candidates(
        db, retag_all=args.all, limit=args.limit, pub_filter=args.pub,
    )
    print(f"Re-tagging scope: {len(candidates)} article(s).")
    if not candidates:
        return 0

    # Show preview of current tag distribution
    by_old = Counter(c["old_primary"] for c in candidates)
    print(f"  current tag distribution: {dict(by_old)}")
    print(f"  mode: {'DRY-RUN (no writes)' if args.dry_run else 'APPLY'}")
    print()

    changes: dict[tuple[str, str], int] = {}   # (old_primary, new_primary): count
    n_changed = 0
    n_unchanged = 0
    n_failed = 0
    t_start = time.time()

    for i, c in enumerate(candidates, 1):
        out = _classify(llm, c)
        if out is None:
            n_failed += 1
            continue

        old = c["old_primary"]
        new = out["jtbd_primary"]
        key = (old, new)
        changes[key] = changes.get(key, 0) + 1

        if old == new:
            n_unchanged += 1
        else:
            n_changed += 1
            logger.info(
                "  article %d: %-18s → %-18s  [%s] %s",
                c["id"], old, new, c["publication"],
                (c["title"] or "")[:60],
            )
            if not args.dry_run:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE article_scores "
                        "   SET jtbd_primary = ?, jtbd_secondary = ? "
                        " WHERE article_id = ?",
                        (new, out["jtbd_secondary"], c["id"]),
                    )

        # Progress every 50
        if i % 50 == 0:
            elapsed = time.time() - t_start
            rate = i / max(elapsed, 0.1)
            eta = (len(candidates) - i) / max(rate, 0.01)
            print(
                f"  ... {i}/{len(candidates)}  "
                f"({rate:.1f}/sec, ETA {eta/60:.1f}min)  "
                f"changed={n_changed} unchanged={n_unchanged} failed={n_failed}"
            )

    elapsed = time.time() - t_start
    print()
    print(f"Done in {elapsed/60:.1f} minutes.")
    print(f"  changed:   {n_changed}")
    print(f"  unchanged: {n_unchanged}")
    print(f"  failed:    {n_failed}")
    print()
    print("Transitions (old → new):")
    for (old, new), n in sorted(changes.items(), key=lambda kv: -kv[1]):
        marker = "  " if old == new else "→ "
        print(f"  {marker}{old:18s} → {new:18s}  n={n}")

    if args.dry_run:
        print()
        print("(DRY-RUN — no writes were made. Re-run without --dry-run to apply.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
