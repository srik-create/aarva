"""Aarva — daily-edition orchestrator.

The top-level command. Runs the pipeline end-to-end, or a single stage in
isolation via --stage.

Day 1: only Stage 1 (ingestion) is implemented. Other stages will be filled in
on subsequent days.

Usage:
    python -m aarva.daily --stage 1
    python -m aarva.daily --stage 1 --pub "Aeon" --pub "ProPublica"   # limit to specific pubs
    python -m aarva.daily                                              # full pipeline (later)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages import (
    stage_1_ingest, stage_1_5_consolidate, stage_2_filter, stage_4_5_6_score,
    stage_7_assemble, stage_8_hook_context,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--stage", type=int, default=None,
              help="Run only this stage number. Omit to run the full pipeline.")
@click.option("--pub", "pubs", multiple=True,
              help="Limit ingestion to this publication name. Repeatable.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug-level logging.")
def main(stage: Optional[int], pubs: tuple[str, ...], verbose: bool) -> None:
    """Run the Aarva daily-edition pipeline."""
    _setup_logging(verbose)
    log = logging.getLogger("aarva.daily")

    config = load_pipeline_config()
    db = Database(config.db_path)

    publication_filter = set(pubs) if pubs else None

    # Stage 1 — Ingestion
    if stage is None or stage == 1:
        log.info("Stage 1 — Ingestion starting")
        run_id = db.start_run("stage_1_ingest")
        try:
            stats = stage_1_ingest.ingest_today(
                config, db, publication_filter=publication_filter
            )
            db.finish_run(
                run_id, status="success",
                articles_ingested=stats.inserted,
            )
            log.info(
                "Stage 1 done — %d publications, %d entries seen, %d new, %d already known, %d extraction failures",
                stats.publications_processed, stats.entries_seen,
                stats.inserted, stats.already_known, stats.extraction_failed,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 1 failed")
            sys.exit(1)

    # Stage 1.5 — Consolidation
    if stage is None or stage == 15:
        log.info("Stage 1.5 — Consolidation starting")
        run_id = db.start_run("stage_1_5_consolidate")
        try:
            cstats = stage_1_5_consolidate.consolidate(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 1.5 done — %d candidates, %d clusters (%d singletons), "
                "%d survivors, %d filtered out",
                cstats.candidates, cstats.clusters_formed, cstats.singletons,
                cstats.survivors, cstats.filtered_out,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 1.5 failed")
            sys.exit(1)

    # Stage 2 — Hard filters
    if stage is None or stage == 2:
        log.info("Stage 2 — Hard filters starting")
        run_id = db.start_run("stage_2_filter")
        try:
            fstats = stage_2_filter.filter_hard(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 2 done — %d candidates, %d below word floor, %d listicles, %d survivors",
                fstats.candidates, fstats.failed_word_floor,
                fstats.failed_listicle, fstats.survivors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 2 failed")
            sys.exit(1)

    # Stage 4+5+6 — Combined LLM scoring
    if stage is None or stage in (4, 456):
        log.info("Stage 4+5+6 — Combined LLM scoring starting")
        run_id = db.start_run("stage_4_5_6_score")
        try:
            sstats = stage_4_5_6_score.score_all(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 4+5+6 done — %d candidates, %d scored (%d PASS, %d FAIL), %d errors",
                sstats.candidates, sstats.scored, sstats.passed, sstats.failed, sstats.errors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 4+5+6 failed")
            sys.exit(1)

    # Stage 7 — Edition assembly
    if stage is None or stage == 7:
        log.info("Stage 7 — Edition assembly starting")
        run_id = db.start_run("stage_7_assemble")
        try:
            astats = stage_7_assemble.assemble_edition(config, db)
            db.finish_run(run_id, status="success")
            if astats.edition_id is not None:
                log.info(
                    "Stage 7 done — edition #%d, %d slots filled, %d skipped (%s)",
                    astats.edition_id, astats.slots_filled,
                    len(astats.slots_skipped),
                    ", ".join(astats.slots_skipped) if astats.slots_skipped else "none",
                )
            else:
                log.warning("Stage 7 done — no edition built (insufficient candidates)")
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 7 failed")
            sys.exit(1)

    # Stage 8 — Hook + why-now contextualisation
    if stage is None or stage == 8:
        log.info("Stage 8 — Hook + why-now contextualisation starting")
        run_id = db.start_run("stage_8_hook_context")
        try:
            s8stats = stage_8_hook_context.generate_for_edition(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 8 done — %d pieces, %d hooks, %d contexts, %d skipped, %d errors",
                s8stats.pieces_total, s8stats.hooks_generated,
                s8stats.contexts_generated, s8stats.skipped_already_done,
                s8stats.errors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 8 failed")
            sys.exit(1)

    if stage is not None and stage not in (1, 15, 2, 4, 456, 7, 8):
        log.warning("Stage %d is not yet implemented (Day %d work).",
                    stage, _stage_to_day(stage))
        sys.exit(2)

    # Summary at end of run.
    counts = db.count_articles_by_status()
    if counts:
        log.info("Article status totals in DB:")
        for status, n in sorted(counts.items()):
            log.info("  %-20s %d", status, n)


def _stage_to_day(stage: int) -> int:
    """Map pipeline stage number to the build day it lands on."""
    return {
        1: 1,
        2: 2,    # Stage 1.5 + Stage 2
        4: 3, 5: 3, 6: 3,
        7: 4,
        8: 5,
        9: 6,
    }.get(stage, 0)


if __name__ == "__main__":
    main()
