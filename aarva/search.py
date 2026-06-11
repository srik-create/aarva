"""Search the article store — lexical, semantic, or both, plus
structured filters by publication / lens / JTBD / status / date.

Usage:
    python -m aarva.search "bukele"                     # lexical, title+excerpt
    python -m aarva.search "bukele" --full-text         # also search body
    python -m aarva.search "institutional decline" --semantic
    python -m aarva.search --pub smithsonian --jtbd delight
    python -m aarva.search "ai" --lens future_gazing --since 2026-05-01
    python -m aarva.search --jtbd smart_escape --limit 50 --json

Any combination of filters is allowed. The positional `query`
argument is optional — without it, the command becomes "list articles
matching the filters" sorted by ranking_score.

Search modes:
  - LEXICAL (default): case-insensitive substring match on title +
    excerpt. Add --full-text to also search the article body (slower,
    broader matches).
  - SEMANTIC (--semantic): embed the query via the configured
    embedding client (same model the articles were embedded with),
    rank by cosine similarity. Articles without embeddings are
    skipped from the results.

Output is a CLI table by default. Use --json to emit a JSON array
suitable for piping into another tool.
"""
from __future__ import annotations

import argparse
import json as _json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Allow running as a script.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from aarva.clients.embedding import build_embedding_client
from aarva.config import load_pipeline_config
from aarva.db import Database


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE, CYAN  # noqa: F401


# ─── Loading ─────────────────────────────────────────────────────────────

_VALID_LENS = {"future_gazing", "humans_and_humanity", "behind_the_news",
               "unclassified"}
_VALID_JTBD = {"keep_up_to_date", "keep_ahead", "curiosity",
               "smart_escape", "delight"}
_VALID_STATUS = {"ingested", "filtered_out", "scored", "in_edition",
                 "extraction_failed"}


def _build_filtered_pool(
    db: Database,
    pub_substr: Optional[str],
    lens: Optional[str],
    jtbd: Optional[str],
    status: Optional[str],
    since: Optional[str],
    lexical_query: Optional[str],
    full_text_search: bool,
) -> list[dict]:
    """Pull articles matching the structural filters. The optional
    lexical_query applies an additional substring filter (case-
    insensitive) against title + excerpt (and full_text if requested).
    """
    where: list[str] = []
    params: list[Any] = []

    if pub_substr:
        where.append("LOWER(p.name) LIKE ?")
        params.append(f"%{pub_substr.lower()}%")
    if lens:
        where.append("s.lens = ?")
        params.append(lens)
    if jtbd:
        where.append("(s.jtbd_primary = ? OR s.jtbd_secondary = ?)")
        params.append(jtbd)
        params.append(jtbd)
    if status:
        where.append("a.status = ?")
        params.append(status)
    if since:
        where.append("COALESCE(a.published_date, a.ingested_date) >= ?")
        params.append(since)
    if lexical_query:
        like = f"%{lexical_query.lower()}%"
        if full_text_search:
            where.append(
                "(LOWER(a.title) LIKE ? OR LOWER(a.excerpt) LIKE ? "
                " OR LOWER(a.full_text) LIKE ?)"
            )
            params += [like, like, like]
        else:
            where.append(
                "(LOWER(a.title) LIKE ? OR LOWER(a.excerpt) LIKE ?)"
            )
            params += [like, like]

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT a.id, a.title, a.byline, a.excerpt,
               a.canonical_url,
               a.published_date, a.ingested_date,
               a.word_count, a.embedding,
               p.name AS publication,
               s.lens, s.jtbd_primary, s.jtbd_secondary,
               COALESCE(s.ranking_score, 0.0) AS ranking_score,
               a.status
          FROM articles a
          JOIN publications p ON p.id = a.publication_id
          LEFT JOIN article_scores s ON s.article_id = a.id
        {where_sql}
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─── Semantic ranking ────────────────────────────────────────────────────

def _semantic_rank(
    pool: list[dict],
    query: str,
    embedding_cfg: dict,
) -> list[tuple[float, dict]]:
    """Embed the query, score each pool article by cosine similarity
    to it. Returns (similarity, article) tuples sorted descending.
    Articles without embeddings are skipped — semantic ranking can't
    place them. To surface them, run a lexical search instead."""
    client = build_embedding_client(embedding_cfg)
    qvec = client.embed([query])[0]
    qnorm = float(np.linalg.norm(qvec))
    if qnorm == 0:
        return []
    ranked: list[tuple[float, dict]] = []
    for a in pool:
        emb_bytes = a.get("embedding")
        if not emb_bytes:
            continue
        try:
            avec = np.frombuffer(emb_bytes, dtype=np.float32)
        except Exception:
            continue
        anorm = float(np.linalg.norm(avec))
        if anorm == 0:
            continue
        sim = float(np.dot(qvec, avec) / (qnorm * anorm))
        ranked.append((sim, a))
    ranked.sort(key=lambda kv: kv[0], reverse=True)
    return ranked


# ─── Output ──────────────────────────────────────────────────────────────

def _snippet(text: str, query: Optional[str], width: int = 140) -> str:
    """Short snippet around the first query occurrence (if any),
    otherwise the start of the excerpt. Whitespace-normalised."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    if query:
        q_low = query.lower()
        idx = text.lower().find(q_low)
        if idx >= 0:
            start = max(0, idx - width // 3)
            end = min(len(text), idx + len(query) + (width - width // 3))
            snip = text[start:end]
            if start > 0:
                snip = "…" + snip
            if end < len(text):
                snip = snip + "…"
            return snip
    return text[:width] + ("…" if len(text) > width else "")


def _format_date(s: Any) -> str:
    if not s:
        return ""
    if isinstance(s, datetime):
        return s.strftime("%Y-%m-%d")
    s = str(s)
    return s[:10]  # ISO-prefix


def _print_results(
    rows: list[tuple[Optional[float], dict]],
    query: Optional[str],
    mode: str,
    total_pool: int,
    json_mode: bool,
) -> None:
    """Render the ranked rows. `rows` is (score-or-None, article-dict).
    For lexical results score is None; the table shows ranking_score
    instead."""
    if json_mode:
        out = []
        for score, a in rows:
            out.append({
                "id": a["id"],
                "title": a["title"],
                "publication": a["publication"],
                "canonical_url": a["canonical_url"],
                "byline": a["byline"],
                "published_date": _format_date(a["published_date"]),
                "lens": a["lens"],
                "jtbd_primary": a["jtbd_primary"],
                "jtbd_secondary": a["jtbd_secondary"],
                "ranking_score": a["ranking_score"],
                "similarity": score,
                "status": a["status"],
                "word_count": a["word_count"],
            })
        print(_json.dumps(out, indent=2, ensure_ascii=False))
        return

    print()
    print(BOLD("═" * 80))
    n = len(rows)
    mode_label = "semantic" if mode == "semantic" else "lexical"
    print(BOLD(f"  Aarva search · {mode_label} · "
               f"{n} result{'s' if n != 1 else ''} of {total_pool} after filters"))
    print(BOLD("═" * 80))

    for i, (score, a) in enumerate(rows, 1):
        date_str = _format_date(a["published_date"]) or _format_date(a["ingested_date"])
        score_str = (f"sim={score:.2f}" if score is not None
                     else f"rs={a['ranking_score']:.2f}")
        jtbd = a["jtbd_primary"] or "-"
        if a["jtbd_secondary"]:
            jtbd = f"{jtbd}/{a['jtbd_secondary']}"
        head = (
            f"  {BOLD(f'[{i}]')}  {YELLOW(score_str)}  "
            f"{BLUE(a['publication'] or '?')}  "
            f"{DIM(date_str)}  {DIM('· ' + jtbd)}  "
            f"{DIM('id=' + str(a['id']))}"
        )
        print()
        print(head)
        print(f"       {BOLD(a['title'] or '(untitled)')}")
        if a["byline"]:
            print(f"       {DIM('by ' + a['byline'])}")
        snip = _snippet(a["excerpt"] or "", query, width=160)
        if snip:
            wrapped = textwrap.fill(snip, width=78,
                                    initial_indent="       ",
                                    subsequent_indent="       ")
            print(DIM(wrapped))
        if a["canonical_url"]:
            print(f"       {CYAN(a['canonical_url'])}")
    print()


# ─── Entry point ─────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", nargs="?", default=None,
                    help="Search term. Without --semantic, matches title+excerpt "
                         "(case-insensitive substring). With --semantic, used as "
                         "the query embedding for cosine similarity ranking. "
                         "Optional — omit to just filter without a search term.")
    ap.add_argument("--semantic", action="store_true",
                    help="Rank by embedding cosine similarity instead of "
                         "lexical substring match.")
    ap.add_argument("--full-text", action="store_true",
                    help="Lexical mode: also search the article body, not "
                         "just title+excerpt. Slower; finds more.")
    ap.add_argument("--pub", type=str, default=None,
                    help="Filter to publications whose name contains this "
                         "substring (case-insensitive).")
    ap.add_argument("--lens", type=str, default=None,
                    help=f"Filter by lens. One of: {sorted(_VALID_LENS)}")
    ap.add_argument("--jtbd", type=str, default=None,
                    help=f"Filter by JTBD (primary OR secondary). "
                         f"One of: {sorted(_VALID_JTBD)}")
    ap.add_argument("--status", type=str, default=None,
                    help=f"Filter by article status. One of: "
                         f"{sorted(_VALID_STATUS)}")
    ap.add_argument("--since", type=str, default=None,
                    help="Filter to articles published/ingested on or after "
                         "this date (YYYY-MM-DD).")
    ap.add_argument("--limit", type=int, default=20,
                    help="Cap results (default 20). Use 0 for no cap.")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON instead of a table.")
    ap.add_argument("--publish", action="store_true",
                    help="After showing results, publish them as bonus "
                         "episodes via aarva.publish_articles. Confirms "
                         "before writing. Combine with --limit to cap the "
                         "set, e.g. `--limit 3 --publish`.")
    ap.add_argument("--publish-force", action="store_true",
                    help="When publishing, override the in-edition / FAIL "
                         "guards. Implies --publish.")
    args = ap.parse_args(argv)

    # Validate enum-like args
    if args.lens and args.lens not in _VALID_LENS:
        print(RED(f"--lens must be one of: {sorted(_VALID_LENS)}"))
        return 1
    if args.jtbd and args.jtbd not in _VALID_JTBD:
        print(RED(f"--jtbd must be one of: {sorted(_VALID_JTBD)}"))
        return 1
    if args.status and args.status not in _VALID_STATUS:
        print(RED(f"--status must be one of: {sorted(_VALID_STATUS)}"))
        return 1
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(RED("--since must be YYYY-MM-DD"))
            return 1
    if args.semantic and not args.query:
        print(RED("--semantic requires a query argument."))
        return 1

    config = load_pipeline_config()
    db = Database(config.db_path)

    # In semantic mode the lexical substring filter is NOT applied —
    # the query is used for similarity instead. The structural filters
    # still apply.
    pool = _build_filtered_pool(
        db,
        pub_substr=args.pub,
        lens=args.lens,
        jtbd=args.jtbd,
        status=args.status,
        since=args.since,
        lexical_query=args.query if not args.semantic else None,
        full_text_search=args.full_text,
    )
    total_pool = len(pool)

    if args.semantic:
        emb_cfg = config.raw.get("embedding", {})
        ranked = _semantic_rank(pool, args.query, emb_cfg)
        results = ranked
        mode = "semantic"
    else:
        # Lexical results sorted by ranking_score descending (best
        # editorial quality first), then by recency as tiebreaker.
        pool.sort(key=lambda a: (
            -float(a.get("ranking_score") or 0.0),
            -(int(a["id"])),
        ))
        results = [(None, a) for a in pool]
        mode = "lexical"

    if args.limit > 0:
        results = results[:args.limit]

    _print_results(results, args.query, mode, total_pool, args.json)

    # --publish handoff. Implies confirmation: shows the user the
    # picked set, then asks before invoking publish_articles.
    want_publish = args.publish or args.publish_force
    if want_publish and results:
        ids = [int(a["id"]) for _, a in results]
        print(BOLD(f"\nAbout to publish {len(ids)} article(s) as bonus episodes:"))
        for _, a in results:
            print(f"  id={a['id']}  [{a['publication']}]  "
                  f"{(a['title'] or '')[:60]}")
        try:
            confirm = input(BOLD("\nProceed? [y/N]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if confirm not in ("y", "yes"):
            print(DIM("Cancelled."))
            return 0
        # Import lazily so the search CLI doesn't pull in TTS/stages
        # for non-publish runs.
        from aarva import publish_articles as _pa
        argv_pa: list[str] = [str(i) for i in ids]
        if args.publish_force:
            argv_pa = ["--force"] + argv_pa
        return _pa.main(argv_pa)
    return 0


if __name__ == "__main__":
    sys.exit(main())
