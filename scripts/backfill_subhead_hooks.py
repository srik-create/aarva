"""Backfill subhead_hook for existing crosscut episodes.

Content-quality Section 2 (docs/session_plan_content_quality.md §2)
added a one-sentence listener-facing sub-heading to crosscut episodes,
generated at build time going forward. This backfills it for episodes
that predate the feature. Idempotent: skips rows where subhead_hook
is already set, so it's safe to re-run.

Scope: main DB only. As of 2026-07-11 (when this script was written)
no episode has ever been built directly into the listener DB (the
listener-DB split shipped 2026-07-06 but nothing has been built there
yet — the one surviving listener episode predates the split and lives
in the main DB). If that changes, this script will need a
listener-DB pass too, but the historical angle_a/angle_b/
connection_summary context this prompt wants only exists in the main
DB's crosscut_pair_candidates table anyway — a listener-DB episode
built via the "new pairing" /create path never had those generated in
the first place (see episode_candidates.py's _PROPOSAL_PROMPT), so a
listener-DB backfill would need a different, thinner prompt built from
intro_text/bridge_between/outro_text instead. Not built here since
there's nothing to backfill yet.

Usage:
    python scripts/backfill_subhead_hooks.py
    python scripts/backfill_subhead_hooks.py --dry-run
    python scripts/backfill_subhead_hooks.py --since 2026-06-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aarva.clients.llm import build_llm_client
from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages.stage_crosscut import _SUBHEAD_HOOK_PROMPT, _generate_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List episodes that would be backfilled; don't write anything.",
    )
    parser.add_argument(
        "--since",
        help="Only backfill episodes with edition_date >= this date (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("backfill_subhead_hooks")

    config = load_pipeline_config()
    db = Database(str(config.db_path))

    where = [
        "e.edition_type = 'crosscut'",
        "(e.subhead_hook IS NULL OR e.subhead_hook = '')",
    ]
    params: list = []
    if args.since:
        where.append("e.edition_date >= ?")
        params.append(args.since)

    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.id AS edition_id, e.edition_date, e.topic_label,
                   e.originating_prompt,
                   cpc.angle_a_label, cpc.angle_b_label,
                   cpc.connection_summary
              FROM editions e
              LEFT JOIN crosscut_pair_candidates cpc ON cpc.edition_id = e.id
             WHERE {' AND '.join(where)}
             ORDER BY e.edition_date DESC
            """,
            params,
        ).fetchall()

    if not rows:
        logger.info("No crosscut episodes need a subhead_hook backfill.")
        return 0

    logger.info("Found %d crosscut episode(s) missing subhead_hook", len(rows))

    if args.dry_run:
        for r in rows:
            print(f"  edition_id={r['edition_id']}  date={r['edition_date']}  topic={r['topic_label']}")
        return 0

    llm = build_llm_client(config.llm)

    errors = 0
    for r in rows:
        cand = dict(r)
        shared_question = (cand.get("connection_summary") or "").split(".")[0] or (cand.get("topic_label") or "")
        prompt_ack = ""
        originating_prompt = cand.get("originating_prompt")
        if originating_prompt:
            prompt_ack = (
                "\n═══════════════════════════════════════════════════════"
                "════════════\nLISTENER'S SEARCH\n"
                "═══════════════════════════════════════════════════════"
                "════════════\n\n"
                f'This episode was built from a listener\'s search: "{originating_prompt}"\n'
                "Tie the hook back to what they asked about — it should "
                "read as engaging their question, not just describing the "
                "pairing in the abstract.\n"
            )

        hook = _generate_text(
            llm, _SUBHEAD_HOOK_PROMPT, temperature=0.6,
            topic_label=cand.get("topic_label") or "",
            shared_question=shared_question,
            angle_a=cand.get("angle_a_label") or "",
            angle_b=cand.get("angle_b_label") or "",
            connection_summary=cand.get("connection_summary") or "",
            prompt_acknowledgment=prompt_ack,
        )
        if isinstance(hook, str):
            hook = hook.strip()

        if not hook:
            logger.warning("  edition_id=%d — generation failed, skipping", cand["edition_id"])
            errors += 1
            continue

        with db.connect() as conn:
            conn.execute(
                "UPDATE editions SET subhead_hook = ? WHERE id = ?",
                (hook, cand["edition_id"]),
            )
        logger.info("  edition_id=%-4d %s  -> %s", cand["edition_id"], cand["edition_date"], hook)

    logger.info(
        "Backfill complete — %d updated, %d errors",
        len(rows) - errors, errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
