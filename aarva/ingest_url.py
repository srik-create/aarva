"""Ad-hoc URL ingestion.

Fetch a specific article URL, extract it, and run it through the same
downstream processing a normal RSS-ingested article gets — Stage 2
hard filters, Stage 4-5-6 scoring, Stage 8.5 author-provenance
classification, embedding generation — scoped to just this one
article via the same article_filter_ids pattern Stage 4-5-6 already
uses. Optionally add the result to today's daily edition.

See docs/session_plan_operator_search_and_url_ingest.md (Feature B).

Usage:
    python -m aarva.ingest_url https://example.com/some/article
    python -m aarva.ingest_url <url1> <url2> ...             # batch
    python -m aarva.ingest_url <url> --add-to-edition         # ingest + add
    python -m aarva.ingest_url <url> --dry-run                # extract, don't persist
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE  # noqa: F401
from aarva.config import load_pipeline_config, load_publications
from aarva.db import Database

_AD_HOC_PUBLICATION_NAME = "Ad hoc"


# ─── Publication domain matching ────────────────────────────────────────
# No existing helper does this — Stage 1's RSS flow always already
# knows which publication it's pulling from (see the research finding
# in docs/session_plan_operator_search_and_url_ingest.md). Written
# fresh here.

def _normalize_netloc(url_or_netloc: str) -> str:
    netloc = url_or_netloc
    if "://" in netloc:
        netloc = urlparse(netloc).netloc
    netloc = netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _match_known_publication(url: str) -> Optional[dict]:
    """Match a URL's domain against publications.yaml's rss_url/homepage
    domains. Returns {'name', 'country'} or None."""
    target = _normalize_netloc(url)
    if not target:
        return None
    for pub in load_publications():
        for candidate in (pub.rss_url, pub.homepage):
            if candidate and _normalize_netloc(candidate) == target:
                return {"name": pub.name, "country": pub.country}
    return None


def _prompt_unknown_publication(url: str) -> str:
    """Unknown-domain prompt. Returns 'a', 'b', or 'c' (abort)."""
    domain = _normalize_netloc(url)
    print()
    print(YELLOW(f"Unknown publication domain: {domain}"))
    print("  (a) One-off — use shared 'Ad hoc' publication")
    print("  (b) Register now (adds a DB row; publications.yaml unchanged)")
    print("  (c) Abort this URL, continue with next")
    try:
        choice = input(BOLD("Choice [a/b/c]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "c"
    return choice if choice in ("a", "b", "c") else "c"


def _resolve_publication_id(db: Database, url: str) -> Optional[int]:
    """Returns a publication_id, or None if the operator aborted."""
    known = _match_known_publication(url)
    if known:
        # Plain lookup, NOT upsert_publication(name=...) — that call
        # would overwrite the existing row's rss_url/homepage/tier with
        # None (nothing else was passed), silently breaking tomorrow's
        # RSS ingestion for this publication until the next YAML sync
        # re-populated it. Caught via real-data verification 2026-07-22.
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM publications WHERE name = ?", (known["name"],),
            ).fetchone()
        if row:
            return int(row["id"])
        # Known in YAML but no DB row yet (hasn't synced via a daily
        # run) — safe to insert fresh here since there's no existing
        # row to clobber.
        return db.upsert_publication(
            name=known["name"], country=known.get("country"),
        )

    choice = _prompt_unknown_publication(url)
    if choice == "a":
        return db.upsert_publication(name=_AD_HOC_PUBLICATION_NAME)
    if choice == "b":
        try:
            name = input(BOLD("  Publication name: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not name:
            print(RED("  No name given — aborting this URL."))
            return None
        try:
            country = input(
                BOLD("  Country tag (us/uk/india, blank for none): ")
            ).strip().lower() or None
        except (EOFError, KeyboardInterrupt):
            print()
            country = None
        if country and country not in ("us", "uk", "india"):
            print(YELLOW(f"  '{country}' isn't us/uk/india — saving with no "
                          f"country tag."))
            country = None
        pub_id = db.upsert_publication(name=name, country=country)
        print(DIM("  Note: to enable ongoing RSS ingest for this "
                  "publication, add it to publications.yaml manually."))
        return pub_id
    return None  # abort


# ─── Per-URL ingestion ───────────────────────────────────────────────────

def _ingest_one(
    config, db: Database, url: str, *, dry_run: bool,
) -> Optional[int]:
    """Fetch, extract, classify, score, embed one URL. Returns the new
    article_id, or None if this URL was skipped/failed/aborted."""
    from aarva.sources.article_extractor import extract_article

    if db.article_exists(url):
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = ?", (url,),
            ).fetchone()
        print(YELLOW(f"Already ingested: {url} (id={row['id']}) — skipping "
                      f"re-extraction."))
        return int(row["id"])

    print()
    print(BOLD(f"Fetching {url} ..."))
    extracted = extract_article(
        url,
        timeout=config.ingestion.http_timeout_seconds,
        user_agent=config.ingestion.user_agent,
    )
    if not extracted:
        print(RED(f"  Extraction failed (paywall, JS-rendered, dead link, "
                   f"or too little text) — skipping."))
        return None

    # Metadata (title/byline/date) isn't part of extract_article's
    # return value — that's normally supplied by the RSS feed entry,
    # which doesn't exist for an ad-hoc URL. trafilatura's own metadata
    # extractor covers it; verified against a real ProPublica URL
    # during implementation (title/author/date all populated).
    import trafilatura
    html = trafilatura.fetch_url(url)
    title, byline, published_date = url, None, None
    if html:
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
            if meta:
                d = meta.as_dict()
                title = d.get("title") or title
                byline = d.get("author") or None
                published_date = d.get("date") or None
        except Exception as e:
            print(DIM(f"  (metadata extraction failed: {e} — using URL as "
                       f"title placeholder)"))

    full_text, stripped = _strip_boilerplate(extracted.full_text)
    if stripped:
        print(DIM(f"  Stripped {len(stripped)} terminal boilerplate "
                   f"paragraph(s)."))
    word_count = len(full_text.split())

    if dry_run:
        print(GREEN(f"  [dry-run] Would ingest: {title!r}  "
                     f"({word_count} words)"))
        return None

    publication_id = _resolve_publication_id(db, url)
    if publication_id is None:
        print(YELLOW(f"  Aborted: {url}"))
        return None

    article_id = db.insert_article(
        canonical_url=url,
        title=title,
        byline=byline,
        publication_id=publication_id,
        published_date=published_date,
        word_count=word_count,
        full_text=full_text,
        excerpt=extracted.excerpt,
        status="ingested",
    )
    if article_id is None:
        print(RED(f"  Insert failed (duplicate canonical_url race?) — "
                   f"skipping."))
        return None

    _run_downstream_stages(config, db, article_id)

    print(GREEN(f"  ✓ id={article_id}  {title}"))
    _print_result_summary(db, article_id)
    return article_id


def _strip_boilerplate(full_text: str) -> tuple[str, list[tuple[str, str]]]:
    from aarva.services.terminal_boilerplate import strip_terminal_boilerplate
    return strip_terminal_boilerplate(full_text)


def _run_downstream_stages(config, db: Database, article_id: int) -> None:
    """Stage 2 (filters) -> Stage 4-5-6 (scoring) -> Stage 8.5 (author
    provenance) -> embedding, all scoped to just this one article_id.
    Mirrors the daily pipeline's own stage sequence; reuses each
    stage's real implementation rather than re-deriving the logic."""
    from aarva.stages.stage_2_filter import filter_hard
    from aarva.stages.stage_4_5_6_score import score_all

    print(DIM("  Running Stage 2 (filters)..."))
    filter_hard(config, db, article_filter_ids={article_id})

    with db.connect() as conn:
        status_row = conn.execute(
            "SELECT status FROM articles WHERE id = ?", (article_id,),
        ).fetchone()
    if status_row and status_row["status"] == "filtered_out":
        print(YELLOW("  Filtered out by Stage 2's structural filters "
                      "(word floor / listicle / digest pattern) — "
                      "stopping here. The row stays in the DB "
                      "(status='filtered_out') if you want to override "
                      "manually."))
        return

    print(DIM("  Running Stage 4-5-6 (scoring)..."))
    score_all(config, db, article_filter_ids={article_id})

    print(DIM("  Running Stage 8.5 (author provenance)..."))
    _classify_author_provenance_one(config, db, article_id)

    print(DIM("  Generating embedding..."))
    _embed_one(config, db, article_id)


def _classify_author_provenance_one(config, db: Database, article_id: int) -> None:
    from aarva.clients.llm import build_llm_client
    from aarva.stages.stage_8c_author_provenance import classify_author_provenance

    with db.connect() as conn:
        row = conn.execute(
            "SELECT byline, full_text FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    if not row:
        return
    llm = build_llm_client(config.llm)
    code = classify_author_provenance(dict(row), llm)
    with db.connect() as conn:
        conn.execute(
            "UPDATE articles SET author_country_code = ? WHERE id = ?",
            (code, article_id),
        )


def _embed_one(config, db: Database, article_id: int) -> None:
    """Skips Stage 1.5's clustering/dedup entirely — that's about
    deduping near-identical articles from the same day's RSS batch,
    which doesn't apply to a single manually-picked URL. Just computes
    + persists the embedding via the same helper Stage 1.5 uses."""
    from aarva.clients.embedding import build_embedding_client
    from aarva.stages.stage_1_5_consolidate import _ArticleRow, _ensure_embeddings

    with db.connect() as conn:
        row = conn.execute("""
            SELECT a.id, a.title, COALESCE(a.excerpt, '') AS excerpt,
                   COALESCE(a.word_count, 0) AS word_count,
                   a.publication_id, p.tier
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.id = ?
        """, (article_id,)).fetchone()
    if not row:
        return
    article_row = _ArticleRow(
        id=int(row["id"]), title=row["title"] or "", excerpt=row["excerpt"],
        word_count=int(row["word_count"]), publication_id=int(row["publication_id"]),
        tier=row["tier"],
    )
    client = build_embedding_client(config.raw.get("embedding", {}))
    _ensure_embeddings(db, client, [article_row])


def _print_result_summary(db: Database, article_id: int) -> None:
    with db.connect() as conn:
        row = conn.execute("""
            SELECT a.title, a.word_count, a.status, a.author_country_code,
                   p.name AS publication,
                   s.rigour, s.posture, s.jtbd_primary, s.lens
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE a.id = ?
        """, (article_id,)).fetchone()
    if not row:
        return
    tags = []
    if row["lens"]:
        tags.append(f"lens={row['lens']}")
    if row["jtbd_primary"]:
        tags.append(f"JTBD={row['jtbd_primary']}")
    if row["rigour"] is not None:
        tags.append(f"rigour={row['rigour']:.2f}")
    if row["author_country_code"]:
        tags.append(f"author_country={row['author_country_code']}")
    tag_str = "  ".join(tags) if tags else "(not yet scored)"
    print(f"    {DIM(row['publication'])}  {DIM(str(row['word_count']) + 'w')}"
          f"  {DIM('status=' + row['status'])}")
    print(f"    {DIM(tag_str)}")


# ─── Entry point ─────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("urls", nargs="+", help="One or more article URLs to ingest.")
    ap.add_argument("--add-to-edition", action="store_true",
                    help="After successfully ingesting, add each article to "
                         "today's daily edition (aarva.services.edition_ops).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract and show what would be ingested; don't "
                         "persist anything.")
    args = ap.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)

    ingested_ids: list[int] = []
    for url in args.urls:
        article_id = _ingest_one(config, db, url, dry_run=args.dry_run)
        if article_id is not None:
            ingested_ids.append(article_id)

    if args.dry_run:
        return 0

    if args.add_to_edition and ingested_ids:
        from aarva.services.edition_ops import add_article_to_todays_edition
        print()
        print(BOLD("Adding to today's edition:"))
        for article_id in ingested_ids:
            result = add_article_to_todays_edition(db, article_id)
            if result == "added":
                print(f"  {GREEN('✓')} id={article_id} added (proposed).")
            elif result == "already_present":
                print(f"  {DIM('·')} id={article_id} already in today's "
                      f"edition — no-op.")
            else:
                print(f"  {RED('✗')} id={article_id}: no daily edition "
                      f"exists for today yet (Stage 7 hasn't run).")

    print()
    print(f"Done — {len(ingested_ids)}/{len(args.urls)} URL(s) ingested.")
    return 0 if ingested_ids or not args.urls else 1


if __name__ == "__main__":
    sys.exit(main())
