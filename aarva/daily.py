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
from aarva.output import web_renderer, rss_feed, audio_converter, r2_uploader
from aarva.sources import curation_crawler, trend_crawler
from aarva.services import article_virality, trend_maintenance, trend_matcher
from aarva.stages import (
    stage_1_ingest, stage_1_5_consolidate, stage_2_filter, stage_4_5_6_score,
    stage_7_assemble, stage_8_hook_context, stage_8c_author_provenance,
    stage_9_tts, stage_crosscut,
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
@click.option("--crosscut-detect", is_flag=True, default=False,
              help="Run only the Crosscut pair-detection stage. Produces "
                   "today's longlist of ~10 pair candidates for review via "
                   "`python -m aarva.crosscut`. Other stages skipped.")
@click.option("--require-fresh", is_flag=True, default=False,
              help="With --crosscut-detect: require at least one article "
                   "in each pair to be one that has NEVER appeared in any "
                   "past longlist. Forces variety when the pool churns slowly.")
@click.option("--crosscut-build", is_flag=True, default=False,
              help="After a pair has been selected via `python -m aarva.crosscut`, "
                   "this runs Phase 3 — generates intro/bridge/outro/key-"
                   "passages via Gemini and creates a crosscut edition row "
                   "for today. Other stages skipped.")
@click.option("--crosscut-tts", is_flag=True, default=False,
              help="Phase 4 — synthesize audio for the most recent built "
                   "crosscut episode (intro+bridges+passages+outro into one "
                   "WAV). Other stages skipped.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug-level logging.")
def main(stage: Optional[int], pubs: tuple[str, ...], crosscut_detect: bool,
         require_fresh: bool, crosscut_build: bool, crosscut_tts: bool,
         verbose: bool) -> None:
    """Run the Aarva daily-edition pipeline."""
    _setup_logging(verbose)
    log = logging.getLogger("aarva.daily")

    config = load_pipeline_config()
    db = Database(config.db_path)

    publication_filter = set(pubs) if pubs else None

    # ── Crosscut: pair-detection-only path ────────────────────────────────
    if crosscut_detect:
        log.info("Crosscut pair detection starting%s",
                 " (require-fresh)" if require_fresh else "")
        run_id = db.start_run("stage_crosscut_detect")
        try:
            cstats = stage_crosscut.detect_pair_candidates(
                config, db, require_fresh_article=require_fresh,
            )
            db.finish_run(run_id, status="success")
            log.info(
                "Crosscut detect done — %d articles considered, "
                "%d pre-scored pairs, %d evaluated, %d persisted "
                "(%d skipped for topic recency)",
                cstats.candidates_considered, cstats.pairs_pre_scored,
                cstats.pairs_eval_called, cstats.pairs_persisted,
                cstats.skipped_for_topic_recency,
            )
            log.info(
                "Next steps:\n"
                "  1. Pick a pair from the longlist:  "
                "`python -m aarva.crosscut`\n"
                "  2. Build the crosscut script:      "
                "`python -m aarva.daily --crosscut-build`"
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Crosscut pair detection failed")
            sys.exit(1)
        return

    # ── Crosscut: TTS path ────────────────────────────────────────────────
    if crosscut_tts:
        log.info("Crosscut TTS starting")
        run_id = db.start_run("stage_crosscut_tts")
        try:
            tstats = stage_crosscut.synthesize_crosscut_episode(config, db)
            db.finish_run(run_id, status="success")
            if tstats.edition_id is None:
                log.warning("Crosscut TTS: no crosscut edition to synthesize.")
            else:
                total_min = tstats.total_audio_seconds / 60.0
                log.info(
                    "Crosscut TTS: edition #%d — %d sections, %.1f min total, "
                    "%d errors. Audio at: %s",
                    tstats.edition_id, tstats.sections_synthesized,
                    total_min, tstats.errors, tstats.output_path,
                )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Crosscut TTS failed")
            sys.exit(1)
        return

    # ── Crosscut: script-build path ───────────────────────────────────────
    if crosscut_build:
        log.info("Crosscut script build starting")
        run_id = db.start_run("stage_crosscut_build")
        try:
            bstats = stage_crosscut.build_episode_script(config, db)
            db.finish_run(run_id, status="success")
            if bstats.edition_id is None:
                log.warning("Crosscut build: no edition produced "
                            "(check logs for the reason).")
            else:
                log.info(
                    "Crosscut build: edition #%d ready — intro=%s, "
                    "bridges=%d, outro=%s, passages_loaded=%d.",
                    bstats.edition_id, bstats.intro_generated,
                    bstats.bridges_generated, bstats.outro_generated,
                    bstats.passages_loaded,
                )
                log.info(
                    "Next steps — generate audio + publish:\n"
                    "  1. Daily hooks/contexts/show-notes: "
                    "`python -m aarva.daily --stage 8`\n"
                    "  2. Daily TTS:                       "
                    "`python -m aarva.daily --stage 9`\n"
                    "  3. Crosscut TTS:                    "
                    "`python -m aarva.daily --crosscut-tts`\n"
                    "  4. Render HTML + RSS:               "
                    "`python -m aarva.daily --stage 10`\n"
                    "  5. Deploy to gh-pages:              "
                    "`bash scripts/publish.sh`"
                )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Crosscut script build failed")
            sys.exit(1)
        return

    # Fail fast on R2 misconfiguration/unreachability — before any of
    # the other 9 stages spend time and money, not after. R2 upload
    # itself doesn't happen until Stage 10; without this check, a bad
    # credential only surfaces there (see 2026-07-03 incident notes on
    # r2_uploader.check_r2_connectivity). Only relevant to a full run
    # or an explicit Stage 10 run — the crosscut-only paths above
    # returned already, and other single-stage runs (--stage 1, etc.)
    # never touch R2.
    if stage is None or stage == 10:
        try:
            r2_uploader.check_r2_connectivity(config)
        except Exception as e:
            log.error(
                "R2 connectivity check failed — fix before running the "
                "daily pipeline (R2 upload happens at Stage 10, the very "
                "end, so this would otherwise waste the whole run): %s", e,
            )
            sys.exit(1)

    # Stage 0 — Curation-platform crawl (external "not too niche"
    # signal — see docs/session_plan_curation_platform_signal.md).
    # Deliberately explicit-only: NOT included in a full (stage=None)
    # run. The operator runs `--stage 0` on its own schedule to build
    # up curation_hits and inspect the output before opting in via
    # pipeline.yaml's curation.enabled — matching the spec's rollout
    # plan. Once enabled, Stage 4-5-6 reads whatever's already in
    # curation_hits; it doesn't require Stage 0 to have just run.
    if stage == 0:
        log.info("Stage 0 — Curation crawl starting")
        run_id = db.start_run("stage_0_curation_crawl")
        try:
            crstats = curation_crawler.crawl_curation_sources(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 0 done — %d/%d sources ok, %d items seen, "
                "%d new hits, %d already known",
                crstats.sources_processed,
                crstats.sources_processed + crstats.sources_failed,
                crstats.items_seen, crstats.hits_added,
                crstats.hits_already_seen,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 0 failed")
            sys.exit(1)
        return

    # Stage 3 — Trend-signal crawl + match + reverse-lookup (external
    # delight/timeliness signal — see docs/session_plan_trend_signal_
    # for_delight.md and, for reverse lookup, docs/session_plan_trend_
    # signal_v2.md concept B). Deliberately explicit-only, same posture
    # as Stage 0's curation crawl: NOT included in a full (stage=None)
    # run. Running `--stage 3` IS the opt-in — no separate pipeline.yaml
    # flag (removed 2026-08-13, reaffirmed 2026-08-20 for the v2
    # additions per the same user decision); whatever it finds always
    # surfaces in the next `python -m aarva.review` run's "Trending"
    # sections. Reverse lookup runs in this SAME stage, not a separate
    # one — same 2026-08-20 decision as the sources themselves.
    if stage == 3:
        log.info("Stage 3 — Trend crawl starting")
        run_id = db.start_run("stage_3_trend_crawl")
        try:
            # Auto-dismiss stale unresolved hits BEFORE the fresh crawl —
            # see docs/session_plan_trend_hits_auto_dismiss_stale.md.
            # Real 2026-08-20 incident: with no time filter, every day's
            # crawl only ever added unresolved rows, so review's
            # "Trending" sections grew to an 800+ backlog. Must run
            # first so today's freshly-inserted rows are never touched.
            dstats = trend_maintenance.dismiss_stale_hits(config, db)
            log.info(
                "Stage 3 auto-dismiss done — %d stale trends, "
                "%d stale virality hits dismissed",
                dstats.trends_dismissed, dstats.virality_dismissed,
            )
            crstats = trend_crawler.crawl_trend_sources(config, db)
            log.info(
                "Stage 3 crawl done — %d/%d sources ok, %d trends seen, "
                "%d new hits, %d already known",
                crstats.sources_processed,
                crstats.sources_processed + crstats.sources_failed,
                crstats.trends_seen, crstats.hits_added,
                crstats.hits_already_seen,
            )
            mstats = trend_matcher.match_trends(config, db)
            log.info(
                "Stage 3 match done — %d processed, %d matched, "
                "%d fallback searches run",
                mstats.trends_processed, mstats.matched, mstats.fallback_ran,
            )
            vstats = article_virality.scan_for_virality(config, db)
            log.info(
                "Stage 3 virality scan done — %d articles scanned, "
                "%d new hits, %d already known, %d errors",
                vstats.articles_scanned, vstats.hits_added,
                vstats.hits_already_seen, vstats.scan_errors,
            )
            db.finish_run(run_id, status="success")
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 3 failed")
            sys.exit(1)
        return

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

        # Cold-start review: if review.enabled is true and we're running
        # the full pipeline (stage is None), halt here. The user needs to
        # approve pieces via `python -m aarva.review` before the LLM and
        # TTS stages run on them.
        review_cfg = config.raw.get("review") or {}
        if stage is None and bool(review_cfg.get("enabled", False)):
            log.info(
                "Pipeline halting after Stage 7 — review.enabled=true.\n"
                "Next steps:\n"
                "  1. Review today's daily:             "
                "`python -m aarva.review`\n"
                "  2. Detect crosscut candidates:        "
                "`python -m aarva.daily --crosscut-detect`\n"
                "  3. Pick a crosscut pair:              "
                "`python -m aarva.crosscut`\n"
                "  4. Build the crosscut script:         "
                "`python -m aarva.daily --crosscut-build`\n"
                "  5. Daily hooks/contexts/show-notes:   "
                "`python -m aarva.daily --stage 8`\n"
                "  6. Author-provenance classification:  "
                "`python -m aarva.daily --stage 85`\n"
                "  7. Daily TTS:                         "
                "`python -m aarva.daily --stage 9`\n"
                "  8. Crosscut TTS:                      "
                "`python -m aarva.daily --crosscut-tts`\n"
                "  9. Render HTML + RSS:                 "
                "`python -m aarva.daily --stage 10`\n"
                "  10. Deploy to gh-pages:               "
                "`bash scripts/publish.sh`\n"
                "\n"
                "(Skip steps 2–4 + 8 on days without a crosscut.)"
            )
            return

    # Stage 8 — Hook + why-now contextualisation
    if stage is None or stage == 8:
        log.info("Stage 8 — Hook + why-now contextualisation starting")
        run_id = db.start_run("stage_8_hook_context")
        try:
            s8stats = stage_8_hook_context.generate_for_edition(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 8 done — %d pieces, %d hooks, %d contexts, %d show notes, "
                "%d skipped, %d errors",
                s8stats.pieces_total, s8stats.hooks_generated,
                s8stats.contexts_generated, s8stats.show_notes_generated,
                s8stats.skipped_already_done, s8stats.errors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 8 failed")
            sys.exit(1)

    # Stage 8.5 — Author-provenance classification (feeds Stage 9's
    # accent steering — see docs/session_plan_author_provenance_accents.md).
    # Numbered 85 to mirror the existing "Stage 1.5" -> 15 convention,
    # since --stage is an int and "8c" doesn't map cleanly.
    if stage is None or stage == 85:
        log.info("Stage 8.5 — Author-provenance classification starting")
        run_id = db.start_run("stage_8c_author_provenance")
        try:
            s85stats = stage_8c_author_provenance.classify_pending_articles(config, db)
            db.finish_run(run_id, status="success")
            log.info(
                "Stage 8.5 done — %d candidates, %d classified "
                "(us=%d uk=%d india=%d unknown=%d), %d errors",
                s85stats.candidates, s85stats.classified,
                s85stats.us, s85stats.uk, s85stats.india, s85stats.unknown,
                s85stats.errors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 8.5 failed")
            sys.exit(1)

    # Stage 9 — TTS audio synthesis
    if stage is None or stage == 9:
        log.info("Stage 9 — TTS audio synthesis starting")
        run_id = db.start_run("stage_9_tts")
        try:
            s9stats = stage_9_tts.generate_for_edition(config, db)
            db.finish_run(run_id, status="success")
            total_min = s9stats.total_audio_seconds / 60.0
            log.info(
                "Stage 9 done — %d pieces, %d audio generated (%.1f min), %d errors",
                s9stats.pieces_total, s9stats.audio_generated,
                total_min, s9stats.errors,
            )
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 9 failed")
            sys.exit(1)

    # Stage 10 — Publish (MP3 conversion + HTML + RSS)
    if stage is None or stage == 10:
        log.info("Stage 10 — Publish starting")
        run_id = db.start_run("stage_10_publish")
        try:
            # 1. Convert any unconverted WAVs to MP3 (podcast apps need MP3)
            try:
                cs = audio_converter.convert_all_for_publish(config, db)
                log.info(
                    "Stage 10 audio — %d converted, %d already done, %d source-missing, %d errors",
                    cs.converted, cs.skipped_already_done,
                    cs.skipped_source_missing, cs.errors,
                )
            except RuntimeError as e:
                log.warning("MP3 conversion skipped: %s", e)
                log.warning(
                    "  → Audio will remain as WAV. Podcast apps may reject WAV "
                    "enclosures. Install ffmpeg (brew install ffmpeg) and re-run "
                    "Stage 10 to convert."
                )

            # 1b. Upload converted MP3s to R2, if R2 is enabled. Runs
            # AFTER MP3 conversion (so audio_url already points at the
            # .mp3) and BEFORE RSS generation (so the <enclosure> URLs
            # in feed.xml are immediately valid when the feed publishes).
            # No-ops cleanly when tts.r2.enabled is false / unset.
            #
            # Retries (mirrors what re-running --stage 10 by hand used
            # to accomplish) rather than swallowing the failure — a bad
            # credential is already caught earlier by
            # check_r2_connectivity above, so a failure reaching here
            # is the rarer transient case (network blip, R2 having a
            # bad moment). If every retry still fails, this re-raises
            # into the outer try/except below, which stops BEFORE the
            # RSS write further down — a stale-but-correct feed beats
            # one that ships pointing at unreachable audio (2026-07-03
            # incident: RSS shipped, Apple/YouTube/aarva.app all 404'd
            # until a manual re-run).
            us = r2_uploader.upload_all_pending_with_retries(config, db)
            if us.uploaded or us.errors:
                log.info(
                    "Stage 10 R2 — %d uploaded, %d already in bucket, "
                    "%d source-missing, %d errors",
                    us.uploaded, us.skipped_already_present,
                    us.skipped_source_missing, us.errors,
                )

            # 2. Render the most recent DAILY edition's HTML.
            with db.connect() as conn:
                latest = conn.execute(
                    "SELECT id FROM editions "
                    " WHERE edition_type = 'daily' "
                    " ORDER BY edition_date DESC, id DESC LIMIT 1"
                ).fetchone()
            if latest:
                ws = web_renderer.render_edition_html(config, db, int(latest["id"]))
                log.info("Stage 10 web — edition #%d rendered to %s (%d pieces)",
                         ws.edition_id, ws.html_path, ws.pieces_rendered)
            else:
                log.warning("Stage 10 web — no editions in DB; skipping HTML")

            # 2b. Render ALL crosscut episodes' HTML pages. Per-episode
            # HTML is what the RSS feed's <link> elements point at, so
            # we need pages for every published crosscut, not just the
            # latest. Idempotent — re-renders existing pages with any
            # template changes.
            with db.connect() as conn:
                cc_rows = conn.execute("""
                    SELECT e.id, e.edition_date
                      FROM editions e
                      JOIN edition_pieces ep ON ep.edition_id = e.id
                     WHERE e.edition_type = 'crosscut'
                       AND ep.audio_url IS NOT NULL AND ep.audio_url != ''
                     GROUP BY e.id
                     ORDER BY e.edition_date DESC, e.id DESC
                """).fetchall()
            cc_count = 0
            for cc_row in cc_rows:
                try:
                    cs = web_renderer.render_crosscut_html(
                        config, db, int(cc_row["id"]),
                    )
                    cc_count += 1
                except Exception as e:
                    log.warning("Stage 10 web — crosscut #%d render failed: %s",
                                int(cc_row["id"]), e)
            if cc_count:
                log.info("Stage 10 web — %d crosscut page(s) rendered", cc_count)

            # 2c. Compose latest.html as the unified "today" view:
            # latest daily + today's crosscut (if any) + today's bonus
            # episodes (if any). Runs AFTER the dated daily + crosscut
            # files are written so it can re-use the same rendering.
            try:
                ls = web_renderer.render_latest_html(config, db)
                if ls.html_path:
                    log.info("Stage 10 web — latest.html rendered (%d total pieces)",
                             ls.pieces_rendered)
            except Exception as e:
                log.warning("Stage 10 web — latest.html render failed: %s", e)

            # 3. Always regenerate the unified RSS feed across all editions
            fs = rss_feed.generate_feed(config, db)
            log.info("Stage 10 RSS — %d items written to %s",
                     fs.items_written, fs.feed_path)

            db.finish_run(run_id, status="success")
        except Exception as e:
            db.finish_run(run_id, status="failed", error_message=str(e))
            log.exception("Stage 10 failed")
            sys.exit(1)

    if stage is not None and stage not in (1, 15, 2, 4, 456, 7, 8, 85, 9, 10):
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
