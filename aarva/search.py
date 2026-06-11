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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Allow running as a script.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from aarva.clients.embedding import build_embedding_client
from aarva.config import load_pipeline_config
from aarva.db import Database


# ─── Natural-language → structured filters ───────────────────────────────

_ASK_PROMPT = """\
You translate a user's natural-language search request into a
structured query for the Aarva article store. Return JSON only — no
prose, no markdown fences.

The user is searching ~3000 articles drawn from rigorous publications
(long-form essays, behind-the-news reporting, science, ideas). Each
article has metadata you can filter on:

LENS (one of, or null):
  future_gazing | humans_and_humanity | behind_the_news | unclassified

JTBD — pick one or more (an array; empty means no jtbd filter):
  keep_up_to_date : current-affairs reporting on a main happening
  keep_ahead      : pieces whose CENTRAL thesis is "this is emerging"
  curiosity       : intellectually engaging, doesn't fit other buckets
  smart_escape    : entertaining-but-easy — light features, place
                    writing, food/travel, profiles of interesting
                    characters. Reader posture is "settle in"
  delight         : LIGHT/FUN/PLAYFUL — humour, wit, oddities,
                    charming reporting. Writing itself is fun.

PUBLICATION HINT — null, or a single-word substring to match
  publication names (e.g. "smithsonian", "atlantic", "vox").

TOPIC KEYWORD — null, or a single word/phrase to match against
  article TITLES (case-insensitive substring).

SINCE_DAYS — null, or an integer N meaning "only articles published
  in the last N days". Use small numbers (3-14) when the user signals
  recency ("this week", "lately", "right now"); larger (30-90) for
  "recent"; null for no constraint.

SEMANTIC_QUERY — a short phrase capturing what the user actually
  wants to read, suitable for embedding cosine similarity. Should be
  conceptual — describe the kind of WRITING + SUBJECT, not just
  keywords. Always emit a non-empty semantic_query; it drives the
  ranking.

CONFIDENCE — one of "low" | "medium" | "high":
  - high   : the request has clear constraints (topic, mood,
             publication, or time scope) AND a specific intent.
  - medium : at least one clear constraint, but the search would
             still return many plausible results.
  - low    : essentially "give me anything good" — no topic, no
             mood, no publication, no specific intent. The user
             should narrow before we search.

REASONING — one short sentence explaining your inference. Surfaced
  to the user as a transparency line.

═══════════════════════════════════════════════════════════════════════
EXAMPLES
═══════════════════════════════════════════════════════════════════════

USER: "make me feel cheerful tonight"
JSON: {
  "lens": null,
  "jtbd": ["delight", "smart_escape"],
  "publication_hint": null,
  "topic_keyword": null,
  "since_days": 14,
  "semantic_query": "lighthearted charming writing that makes a reader smile",
  "confidence": "high",
  "reasoning": "User wants a mood-lift, so prefer playful/restorative pieces from the last fortnight."
}

USER: "the deeper story behind Trump's tariff threats"
JSON: {
  "lens": "behind_the_news",
  "jtbd": ["keep_up_to_date"],
  "publication_hint": null,
  "topic_keyword": "tariff",
  "since_days": 30,
  "semantic_query": "trump tariff trade policy threats deeper analysis context",
  "confidence": "high",
  "reasoning": "Current-affairs analysis on a specific recent policy; behind-the-news lens fits."
}

USER: "I want a quiet long read about nature"
JSON: {
  "lens": "humans_and_humanity",
  "jtbd": ["smart_escape"],
  "publication_hint": null,
  "topic_keyword": "nature",
  "since_days": null,
  "semantic_query": "quiet contemplative nature long-form essay slow living",
  "confidence": "high",
  "reasoning": "Restorative reading; the long-read framing rules out news commentary."
}

USER: "anything from the atlantic about AI"
JSON: {
  "lens": null,
  "jtbd": [],
  "publication_hint": "atlantic",
  "topic_keyword": "ai",
  "since_days": null,
  "semantic_query": "artificial intelligence technology essays",
  "confidence": "high",
  "reasoning": "Explicit publication + topic; broad jtbd to avoid over-filtering."
}

USER: "something interesting"
JSON: {
  "lens": null,
  "jtbd": [],
  "publication_hint": null,
  "topic_keyword": null,
  "since_days": null,
  "semantic_query": "engaging interesting writing",
  "confidence": "low",
  "reasoning": "No topic, mood, publication, or time hint — too broad to search usefully."
}

USER: "good stuff"
JSON: {
  "lens": null,
  "jtbd": [],
  "publication_hint": null,
  "topic_keyword": null,
  "since_days": null,
  "semantic_query": "high quality writing worth reading",
  "confidence": "low",
  "reasoning": "Request is vague — no topic, mood, or scope. Ask the user to narrow."
}

═══════════════════════════════════════════════════════════════════════
USER REQUEST
═══════════════════════════════════════════════════════════════════════

{user_request}

Output the JSON object only.
"""


def _parse_ask(llm, request: str) -> dict:
    """Run the NL → structured filter prompt. Returns the parsed dict
    with all fields populated (None for unset). Falls back to a
    permissive parse if Gemini returns garbage."""
    fallback = {
        "lens": None, "jtbd": [], "publication_hint": None,
        "topic_keyword": None, "since_days": None,
        "semantic_query": request,
        "confidence": "low",
        "reasoning": "(parse failed — falling back to raw query as semantic search)",
    }
    try:
        prompt = _ASK_PROMPT.replace("{user_request}", request)
        result = llm.complete(prompt, expect_json=True, temperature=0.3)
    except Exception as e:
        fallback["reasoning"] = f"(parse failed: {type(e).__name__}; falling back)"
        return fallback
    if not isinstance(result, dict):
        return fallback

    # Normalise + validate the response.
    confidence = (result.get("confidence") or "").strip().lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    parsed = {
        "lens": result.get("lens") or None,
        "jtbd": result.get("jtbd") or [],
        "publication_hint": result.get("publication_hint") or None,
        "topic_keyword": result.get("topic_keyword") or None,
        "since_days": result.get("since_days"),
        "semantic_query": (result.get("semantic_query") or request).strip(),
        "confidence": confidence,
        "reasoning": (result.get("reasoning") or "").strip()
                     or "(no reasoning given)",
    }
    # Enum sanity
    if parsed["lens"] and parsed["lens"] not in _VALID_LENS:
        parsed["lens"] = None
    if isinstance(parsed["jtbd"], list):
        parsed["jtbd"] = [j for j in parsed["jtbd"] if j in _VALID_JTBD]
    else:
        parsed["jtbd"] = []
    if isinstance(parsed["since_days"], (int, float)):
        parsed["since_days"] = max(1, int(parsed["since_days"]))
    else:
        parsed["since_days"] = None
    return parsed


def _is_ask_too_vague(parsed: dict) -> bool:
    """True if the parsed ask has no real filtering signal — we'd be
    asking the user to choose from the entire pool.

    Heuristic only: narrowing is real iff at least one of jtbd /
    lens / topic_keyword / publication_hint / since_days is set.
    Gemini's self-flagged `confidence` is kept in the parse output
    (for the display header) but is too pessimistic to use as a
    trigger — it flags asks like "a surprising read" as low-confidence
    even when it extracted a usable jtbd. The heuristic is more
    reliable.
    """
    has_topic = bool(parsed.get("topic_keyword"))
    has_pub = bool(parsed.get("publication_hint"))
    has_lens = bool(parsed.get("lens"))
    has_jtbd = bool(parsed.get("jtbd"))
    has_time = bool(parsed.get("since_days"))
    return not (has_topic or has_pub or has_lens or has_jtbd or has_time)


def _print_vague_suggestions(ask: str, parsed: dict) -> None:
    """Print a friendly nudge to narrow the ask. No search is run."""
    print()
    print(BOLD("─" * 78))
    print(BOLD(f"Your ask was a little broad: {ask!r}"))
    print(DIM(parsed.get("reasoning") or ""))
    print(BOLD("─" * 78))
    print()
    print("Try narrowing along one of these dimensions:")
    print()
    print(f"  {YELLOW('A topic')}      — e.g.")
    print(f"                 --ask {repr('something about AI')}")
    print(f"                 --ask {repr('the climate-tech beat')}")
    print(f"                 --ask {repr('what is happening in China')}")
    print()
    print(f"  {YELLOW('A mood')}       — e.g.")
    print(f"                 --ask {repr('make me feel cheerful')}")
    print(f"                 --ask {repr('something contemplative')}")
    print(f"                 --ask {repr('a surprising read')}")
    print()
    print(f"  {YELLOW('A publication')} — e.g.")
    print(f"                 --ask {repr('anything from The Atlantic')}")
    print(f"                 --ask {repr('something from Smithsonian')}")
    print()
    print(f"  {YELLOW('A time scope')}  — e.g.")
    print(f"                 --ask {repr('the big essays of the past week')}")
    print(f"                 --ask {repr('what mattered today')}")
    print()
    print(f"  {YELLOW('Or combine')}    — e.g.")
    print(f"                 --ask {repr('something cheerful about science')}")
    print(f"                 --ask {repr('a deep read on tariffs from this week')}")
    print()
    print(DIM("(If you really want a broad search anyway, add --ask-broad.)"))


def _print_ask_header(parsed: dict, overrides: dict[str, Any]) -> None:
    """One-line header showing how Gemini parsed the request, plus
    any explicit-flag overrides applied on top."""
    bits = []
    if parsed["jtbd"]:
        label = "jtbd∈{" + ",".join(parsed["jtbd"]) + "}"
        bits.append(label)
    if parsed["lens"]:
        bits.append(f"lens={parsed['lens']}")
    if parsed["publication_hint"]:
        bits.append(f"pub~={parsed['publication_hint']}")
    if parsed["topic_keyword"]:
        bits.append(f"topic~={parsed['topic_keyword']}")
    if parsed["since_days"]:
        bits.append(f"since={parsed['since_days']}d")
    bits.append(f"semantic={parsed['semantic_query'][:50]!r}")

    override_bits = []
    for k, v in overrides.items():
        if v is not None:
            override_bits.append(f"{k}={v} (overridden)")

    print()
    print(DIM("─" * 78))
    print(BOLD("Inferred: ") + DIM("  ".join(bits)))
    if override_bits:
        print(BOLD("Overrides: ") + YELLOW("  ".join(override_bits)))
    print(DIM(parsed["reasoning"]))
    print(DIM("─" * 78))


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

    Default status filter: only 'scored' articles (i.e., publishable
    candidates that passed editorial filters AND haven't already been
    used). The user can broaden via --status (a comma-separated list,
    or 'any' to disable the filter entirely).
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
    # Default = 'scored' only. The principle: if it shows up in
    # search, the user can publish it without further gatekeeping.
    # extraction_failed / filtered_out / in_edition are filtered out
    # by default. --status overrides explicitly.
    if status is None:
        where.append("a.status = ?")
        params.append("scored")
    elif status.lower() != "any":
        names = [s.strip() for s in status.split(",") if s.strip()]
        if len(names) == 1:
            where.append("a.status = ?")
            params.append(names[0])
        else:
            placeholders = ",".join("?" for _ in names)
            where.append(f"a.status IN ({placeholders})")
            params += names
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
    mode_label = {"ask": "ask (NL → filters + semantic)",
                  "semantic": "semantic",
                  "lexical": "lexical"}.get(mode, mode)
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
                    help="Filter by article status. Default is 'scored' "
                         "(publishable candidates only). Pass 'any' to "
                         "include every status, or a comma-separated list "
                         "(e.g. 'scored,in_edition'). Valid values: "
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
    ap.add_argument("--ask", type=str, default=None,
                    help="Natural-language search. Gemini parses the "
                         "sentence into structured filters + a semantic "
                         "query and runs the search. Example: "
                         "--ask \"make me feel cheerful tonight\". "
                         "Explicit --pub / --jtbd / --lens / --since "
                         "flags override the AI's inference.")
    ap.add_argument("--ask-dry-run", action="store_true",
                    help="With --ask, print the parsed filters and exit "
                         "without searching.")
    ap.add_argument("--ask-broad", action="store_true",
                    help="With --ask, run the search even when the parse "
                         "is too vague (no topic / mood / pub / lens). "
                         "Default behaviour for vague asks is to print "
                         "suggestions instead.")
    args = ap.parse_args(argv)

    # Validate enum-like args
    if args.lens and args.lens not in _VALID_LENS:
        print(RED(f"--lens must be one of: {sorted(_VALID_LENS)}"))
        return 1
    if args.jtbd and args.jtbd not in _VALID_JTBD:
        print(RED(f"--jtbd must be one of: {sorted(_VALID_JTBD)}"))
        return 1
    if args.status and args.status.lower() != "any":
        # Allow comma-separated list of statuses.
        names = [s.strip() for s in args.status.split(",") if s.strip()]
        unknown = [n for n in names if n not in _VALID_STATUS]
        if unknown:
            print(RED(f"--status: unknown {unknown}. Valid: "
                      f"{sorted(_VALID_STATUS)} or 'any'"))
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
    if args.ask_dry_run and not args.ask:
        print(RED("--ask-dry-run requires --ask."))
        return 1

    config = load_pipeline_config()
    db = Database(config.db_path)

    # ─── --ask mode: NL → structured filters via Gemini ──────────────
    parsed: Optional[dict] = None
    if args.ask:
        # Lazy import to avoid pulling the LLM client when it's not used.
        from aarva.clients.llm import build_llm_client
        llm = build_llm_client(config.llm)
        parsed = _parse_ask(llm, args.ask)

        # Explicit-flag overrides — user-given flags always win over
        # the AI's inference. Track which fields were overridden so
        # we can show them in the header.
        overrides: dict[str, Any] = {}
        if args.lens:
            overrides["lens"] = args.lens
            parsed["lens"] = args.lens
        if args.jtbd:
            overrides["jtbd"] = args.jtbd
            parsed["jtbd"] = [args.jtbd]
        if args.pub:
            overrides["pub"] = args.pub
            parsed["publication_hint"] = args.pub
        if args.since:
            overrides["since"] = args.since
            parsed["since_days"] = None    # explicit date wins; clear days
        if args.query:
            overrides["semantic_query"] = args.query
            parsed["semantic_query"] = args.query

        _print_ask_header(parsed, overrides)

        # Vague-ask guard: unless the user explicitly asked for a
        # broad search, prompt them to narrow before running.
        # Explicit flag overrides (pub, jtbd, lens, since) count as
        # narrowing — if the user provided any of those, treat the
        # ask as narrow enough.
        any_explicit_narrowing = any([args.pub, args.jtbd, args.lens,
                                       args.since])
        if (_is_ask_too_vague(parsed)
                and not args.ask_broad
                and not any_explicit_narrowing):
            _print_vague_suggestions(args.ask, parsed)
            return 0

        if args.ask_dry_run:
            return 0

        # Map parsed → effective filter values for the pool builder.
        # If multiple JTBDs were inferred (the typical case for moods
        # like "cheerful" → [delight, smart_escape]), we apply the
        # filter at result-rank time rather than as a hard WHERE
        # clause — the pool builder takes only one jtbd. The two-jtbd
        # case is handled by post-filtering the pool below.
        eff_pub = parsed["publication_hint"]
        eff_lens = parsed["lens"]
        eff_jtbds: list[str] = parsed["jtbd"]
        eff_since = args.since
        if not eff_since and parsed["since_days"]:
            eff_since = (
                datetime.now() - timedelta(days=int(parsed["since_days"]))
            ).strftime("%Y-%m-%d")
        eff_query_for_semantic = parsed["semantic_query"]
    else:
        eff_pub = args.pub
        eff_lens = args.lens
        eff_jtbds = [args.jtbd] if args.jtbd else []
        eff_since = args.since
        eff_query_for_semantic = args.query

    # Pool build: single jtbd → push into SQL; multiple → take broader
    # pool and filter after. For --ask without jtbd, no filter at all.
    pool_jtbd = eff_jtbds[0] if len(eff_jtbds) == 1 else None
    # In semantic / --ask mode the lexical substring filter is NOT
    # applied — the query drives ranking, not filtering.
    use_semantic = args.semantic or bool(args.ask)
    pool = _build_filtered_pool(
        db,
        pub_substr=eff_pub,
        lens=eff_lens,
        jtbd=pool_jtbd,
        status=args.status,
        since=eff_since,
        lexical_query=args.query if not use_semantic else None,
        full_text_search=args.full_text,
    )
    # Multi-jtbd post-filter: keep only articles matching any of the
    # inferred buckets (primary OR secondary).
    if len(eff_jtbds) > 1:
        wanted = set(eff_jtbds)
        pool = [
            a for a in pool
            if (a.get("jtbd_primary") in wanted
                or a.get("jtbd_secondary") in wanted)
        ]
    total_pool = len(pool)

    if use_semantic:
        emb_cfg = config.raw.get("embedding", {})
        ranked = _semantic_rank(pool, eff_query_for_semantic, emb_cfg)
        results = ranked
        mode = "ask" if args.ask else "semantic"
    else:
        # Lexical results sorted by ranking_score descending (best
        # editorial quality first), then by recency as tiebreaker.
        pool.sort(key=lambda a: (
            -float(a.get("ranking_score") or 0.0),
            -(int(a["id"])),
        ))
        results = [(None, a) for a in pool]
        mode = "lexical"

    want_publish = args.publish or args.publish_force
    batch_size = args.limit if args.limit > 0 else 20

    # Non-publish path: just show the top-N batch and exit.
    if not want_publish:
        display = results[:args.limit] if args.limit > 0 else results
        _print_results(display, args.query, mode, total_pool, args.json)
        return 0

    # Publish path: interactive picker over the full ranked list. Show
    # one batch at a time, accept numbers/ranges/"all", "more" to
    # paginate, "q" / empty to cancel.
    if not results:
        # _print_results still has a useful "0 results" header.
        _print_results([], args.query, mode, total_pool, args.json)
        return 0

    return _interactive_publish(
        results, args.query, mode, total_pool,
        batch_size=batch_size, force=args.publish_force,
    )


def _interactive_publish(
    full_ranked: list[tuple[Optional[float], dict]],
    query: Optional[str],
    mode: str,
    total_pool: int,
    *,
    batch_size: int,
    force: bool,
) -> int:
    """Pagination + picker loop. Returns shell exit code.

    UX:
      <numbers / ranges / 'all'>  → publish selected indices in the current
                                     batch
      'more'                       → next batch
      'q' / empty                  → cancel
    """
    offset = 0
    while True:
        batch = full_ranked[offset:offset + batch_size]
        if not batch:
            print(DIM("No more results in this search."))
            print(DIM("Refine your --ask or re-run with different filters."))
            return 0

        # Header shows running window (e.g., "20 results 21-40 of 142")
        window_label = (
            f"{len(batch)} results "
            f"{offset + 1}-{offset + len(batch)} of {len(full_ranked)} "
            f"(after filters)"
        )
        # _print_results expects total_pool as an int. We override the
        # header line below; pass len(full_ranked) for the count.
        _print_results(batch, query, mode, total_pool, json_mode=False)

        # Picker prompt
        print(BOLD(f"\nWindow: {window_label}"))
        print(BOLD("Choose articles to publish:"))
        print(f"  {YELLOW('1,3,5')}     publish results 1, 3 and 5")
        print(f"  {YELLOW('1-3')}       publish 1 through 3 (range)")
        print(f"  {YELLOW('all')}       publish everything in this batch")
        print(f"  {YELLOW('more')}      show the next batch")
        print(f"  {YELLOW('q / ↵')}     cancel")
        try:
            choice = input(BOLD("> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1

        if not choice or choice in ("q", "quit"):
            print(DIM("Cancelled."))
            return 0
        if choice == "more":
            offset += batch_size
            continue
        if choice == "all":
            selected = [a for _, a in batch]
        else:
            try:
                indices = _parse_picks(choice, len(batch))
            except ValueError as e:
                print(RED(f"  {e}"))
                continue
            selected = [batch[i - 1][1] for i in indices]

        # Confirm + dispatch to publish_articles.
        print()
        print(BOLD(f"Publishing {len(selected)} article(s) as bonus episodes:"))
        for a in selected:
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

        # Lazy import so non-publish runs don't pull in TTS/stages.
        from aarva import publish_articles as _pa
        argv_pa = [str(int(a["id"])) for a in selected]
        if force:
            argv_pa = ["--force"] + argv_pa
        return _pa.main(argv_pa)


def _parse_picks(choice: str, max_n: int) -> list[int]:
    """Parse user input like '1,3,5' / '1-3' / '1 3 5' / '1, 3-5'
    into a sorted, deduplicated list of 1-based indices. Raises
    ValueError on bad tokens or out-of-range values."""
    cleaned = choice.replace(",", " ").replace(";", " ")
    tokens = cleaned.split()
    if not tokens:
        raise ValueError("empty selection")
    picks: set[int] = set()
    for tok in tokens:
        if "-" in tok and not tok.startswith("-"):
            # Range like "1-3"
            parts = tok.split("-", 1)
            try:
                a, b = int(parts[0]), int(parts[1])
            except ValueError:
                raise ValueError(f"bad range: {tok!r}")
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if not (1 <= i <= max_n):
                    raise ValueError(
                        f"{i} out of range (must be 1-{max_n})"
                    )
                picks.add(i)
        else:
            try:
                i = int(tok)
            except ValueError:
                raise ValueError(f"not a number or range: {tok!r}")
            if not (1 <= i <= max_n):
                raise ValueError(
                    f"{i} out of range (must be 1-{max_n})"
                )
            picks.add(i)
    return sorted(picks)


if __name__ == "__main__":
    sys.exit(main())
