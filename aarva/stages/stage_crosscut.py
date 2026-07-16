"""Stage Crosscut — daily pair-detection for the Crosscut episode type.

Crosscut is the second episode type in Aarva's feed: one piece per day,
two rigorous articles on the same topic from different angles, stitched
together with editorial intro / bridge / outro in Aarva's first-person
voice.

This stage does Phase 2 of the Crosscut pipeline:

  1. Pull all scored articles from the last N days (default 14).
  2. Find candidate pairs: topically similar enough that they share a
     question, structurally divergent enough that they bring different
     angles. We use embedding cosine similarity for the topical signal
     and count axis differences (lens, pillar, jtbd_primary, narrative-
     fingerprint dimensions) for the divergence signal.
  3. Exclude pairs whose topic was used in any of the last N crosscut
     episodes (anti-repetition).
  4. Take top ~30 by structural divergence. Each of those 30 gets a
     stance classification (OPPOSING_VIEWS vs. DIFFERENT_ANGLES — see
     docs/session_plan_users_and_crosscut_upgrades.md Section 2, added
     2026-07-15) and the existing Gemini connection-eval (one-sentence
     rationale + 0-10 quality score).
  5. Persist the top 10 to crosscut_pair_candidates: 60/40 divergent/
     current-logic when divergent (OPPOSING_VIEWS) pairs exist, all
     current-logic otherwise. Editorial voice is unchanged either way
     — this only changes which pairs get selected, never how the
     eventual intro/bridge/outro frame them.
  6. The longlist CLI then displays these for user selection.

The selection itself (user picks 1 of 10) lives in aarva/crosscut.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np

from aarva.clients.llm import build_llm_client, LLMClient
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

# Lookback window — how many days of scored articles we consider for
# pair candidates. Two weeks is enough to find diverse angles without
# letting the feed feel stale.
DEFAULT_LOOKBACK_DAYS = 14

# Topical similarity floor. Below this, the two articles are too
# different in subject for "same question, different angles" to apply.
DEFAULT_TOPICAL_SIM_FLOOR = 0.45

# Topical similarity ceiling. Above this, the two articles are
# essentially duplicates / the same story — Stage 1.5 would have
# clustered them. Not what we want.
DEFAULT_TOPICAL_SIM_CEILING = 0.85

# Maximum candidate pairs that get sent to Gemini for the connection
# eval. We pre-score by structural divergence and take this many.
# Higher = more thorough but more LLM cost.
DEFAULT_MAX_EVAL_CANDIDATES = 30

# Final longlist size shown to the user.
DEFAULT_LONGLIST_SIZE = 10

# Per-article appearance cap on the longlist. Each article appears at
# most this many times across the longlist. 1 forces real diversity —
# every pair brings two pieces the user hasn't seen elsewhere in the
# longlist. 2 (the previous value) let a few high-rigour articles
# dominate the longlist by pairing with everything topically adjacent.
DEFAULT_MAX_APPEARANCES_PER_ARTICLE = 1

# Window for excluding previously-selected pairs. Any (article_a, article_b)
# pair that's been selected for a past crosscut episode in this window
# is skipped during pre-scoring. Prevents the algorithm from re-proposing
# pairs we've already engaged with.
DEFAULT_SELECTED_PAIR_EXCLUSION_DAYS = 60

# Anti-repetition: skip candidate pairs whose topic matches any of the
# topics used in the last N crosscut episodes. We compare on the
# topic_label LLM-output from those past episodes.
DEFAULT_TOPIC_RECENCY_WINDOW = 3


@dataclass
class CrosscutPairStats:
    candidates_considered: int = 0
    pairs_pre_scored: int = 0
    pairs_eval_called: int = 0
    pairs_persisted: int = 0
    skipped_for_topic_recency: int = 0
    pairs_stance_classified: int = 0
    pairs_divergent_found: int = 0


# ─── Article loading ──────────────────────────────────────────────────────

@dataclass
class _CrosscutArticle:
    id: int
    title: str
    publication_name: str
    word_count: int
    excerpt: str
    full_text: str       # used by _eval_pair_via_llm for accurate angle / connection judgments
    embedding: np.ndarray
    lens: Optional[str]
    pillar: Optional[str]
    jtbd_primary: Optional[str]
    fingerprint: dict


def _load_crosscut_pool(
    db: Database, lookback_days: int,
    ranking_score_floor: float = 0.7,
) -> list[_CrosscutArticle]:
    """Load articles eligible for crosscut pair selection.

    Same hard gate as the daily edition (rigour ≥ 0.5 AND posture ≥ 0.5
    via verdict='PASS') PLUS a higher overall-ranking floor — crosscut
    is a featured episode type, so we reserve it for genuinely high-
    rigour pieces, not "good enough for daily" pieces at the bottom of
    the pass-band.

    Floor at 0.7 means the average of rigour and posture is ≥ ~0.78
    (since ranking_score = 0.45*rigour + 0.45*posture + 0.10*self).
    """
    since = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, a.word_count, a.excerpt, a.full_text,
                   a.embedding,
                   p.name AS publication_name,
                   s.lens, s.pillar, s.jtbd_primary, s.fingerprint_json
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
              JOIN article_scores s ON s.article_id = a.id
             WHERE s.verdict = 'PASS'
               AND COALESCE(s.ranking_score, 0) >= ?
               AND a.embedding IS NOT NULL
               AND COALESCE(a.published_date, a.ingested_date) >= ?
               -- Exclude articles already used in a published edition
               -- (daily or crosscut). Stage 7 does this via
               -- `a.status = 'scored'`; the crosscut pool is looser
               -- (also accepts 'in_basket' etc.) so we spell out the
               -- exclusion instead of restricting to a single status.
               AND a.status != 'in_edition'
               -- Exclude articles the reviewer rejected in any past
               -- edition. Mirrors Stage 7's NOT EXISTS clause — the
               -- rejection block is durable across editions via the
               -- edition_rejections table. Without this, an article
               -- the reviewer said "no" to for the daily can still
               -- surface as a crosscut candidate.
               AND NOT EXISTS (
                   SELECT 1 FROM edition_rejections er
                    WHERE er.article_id = a.id
               )
        """, (ranking_score_floor, since)).fetchall()

    out: list[_CrosscutArticle] = []
    for r in rows:
        try:
            vec = np.frombuffer(r["embedding"], dtype=np.float32)
            fp = json.loads(r["fingerprint_json"] or "{}")
        except (ValueError, json.JSONDecodeError):
            continue
        out.append(_CrosscutArticle(
            id=int(r["id"]),
            title=r["title"] or "",
            publication_name=r["publication_name"] or "",
            word_count=int(r["word_count"] or 0),
            excerpt=r["excerpt"] or "",
            full_text=r["full_text"] or "",
            embedding=vec,
            lens=r["lens"],
            pillar=r["pillar"],
            jtbd_primary=r["jtbd_primary"],
            fingerprint=fp if isinstance(fp, dict) else {},
        ))
    return out


def _recent_crosscut_topics(db: Database, window: int) -> set[str]:
    """Lowercased topic_labels from the last N crosscut episodes. Used
    to skip candidate pairs whose topic we just covered."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT topic_label FROM editions
             WHERE edition_type = 'crosscut'
               AND topic_label IS NOT NULL
             ORDER BY edition_date DESC, id DESC
             LIMIT ?
        """, (window,)).fetchall()
    return {(r["topic_label"] or "").strip().lower() for r in rows}


def _previously_seen_article_ids(db: Database) -> set[int]:
    """Return article IDs that have appeared in ANY crosscut_pair_
    candidates row, including today's. Used by --require-fresh.

    We DO include today's rows because the typical use case is:
    the user ran detect once, didn't like the longlist, runs detect
    again with --require-fresh expecting a different set of articles.
    The seen check runs BEFORE today's rows get wiped (later in the
    same detect call), so today's just-shown articles count as 'seen'
    and won't reappear.
    """
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT article_a_id, article_b_id
              FROM crosscut_pair_candidates
        """).fetchall()
    seen: set[int] = set()
    for r in rows:
        seen.add(int(r["article_a_id"]))
        seen.add(int(r["article_b_id"]))
    return seen


def _previously_selected_pairs(db: Database, lookback_days: int) -> set[frozenset]:
    """Return frozensets of {article_a_id, article_b_id} for any pair
    the user has previously seen, selected, or had built into an
    episode within the lookback window.

    Three sources of truth so re-running detect reliably gives the
    user a fresh longlist — same articles can recur in different
    pairings, but no exact pair repeats:
      1. Built episodes — query editions+edition_pieces (source of
         truth; survives candidate-table churn).
      2. Selected but not yet built — crosscut_pair_candidates rows
         where selected_at is set. Covers the gap between selection
         and episode build.
      3. Previously shown in a longlist — every crosscut_pair_candidates
         row in the lookback window, INCLUDING superseded ones. This
         is what makes "I don't like these pairs, give me new ones"
         work: re-running detect will not re-propose pairs the user
         has already scrolled past.
    """
    # IMPORTANT: compare DATE-only strings, not full ISO datetimes.
    # `e.edition_date` and `cpc.candidate_date` are stored as YYYY-MM-DD;
    # if `since` were `2026-04-10T14:23:45.123456` then
    # '2026-04-10' < '2026-04-10T14:23:45' lexicographically — pairs
    # from the boundary day would be silently missed.
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    pairs: set[frozenset] = set()

    with db.connect() as conn:
        # 1. Built crosscut episodes.
        rows = conn.execute("""
            SELECT GROUP_CONCAT(ep.article_id) AS ids
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id
             WHERE e.edition_type = 'crosscut'
               AND e.edition_date >= ?
             GROUP BY e.id
        """, (since,)).fetchall()
        for r in rows:
            ids = [int(x) for x in (r["ids"] or "").split(",") if x]
            if len(ids) >= 2:
                pairs.add(frozenset(ids[:2]))

        # 2. Selected but not yet built candidates.
        rows = conn.execute("""
            SELECT article_a_id, article_b_id
              FROM crosscut_pair_candidates
             WHERE selected_at IS NOT NULL
               AND edition_id IS NULL
               AND selected_at >= ?
        """, (since,)).fetchall()
        for r in rows:
            pairs.add(frozenset({int(r["article_a_id"]),
                                  int(r["article_b_id"])}))

        # 3. Every previously-shown longlist pair (including superseded).
        # This is what lets `--crosscut-detect` act as a regeneration
        # command: re-running it will pick pairs the user hasn't seen yet.
        rows = conn.execute("""
            SELECT article_a_id, article_b_id
              FROM crosscut_pair_candidates
             WHERE candidate_date >= ?
        """, (since,)).fetchall()
        for r in rows:
            pairs.add(frozenset({int(r["article_a_id"]),
                                  int(r["article_b_id"])}))

    return pairs


# ─── Scoring ──────────────────────────────────────────────────────────────

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _divergence(a: _CrosscutArticle, b: _CrosscutArticle) -> int:
    """Count of axes that differ between two articles. Higher = the
    two pieces bring more distinct angles to whatever topic they share."""
    score = 0
    if a.lens != b.lens:                   score += 1
    if a.pillar != b.pillar:               score += 1
    if a.jtbd_primary != b.jtbd_primary:   score += 1
    # Narrative-fingerprint dimensions. Each that differs adds a point.
    for dim in ("structural_form", "method_of_inquiry", "voice_register",
                "cognitive_density", "emotional_register", "temporal_lens"):
        va = (a.fingerprint or {}).get(dim)
        vb = (b.fingerprint or {}).get(dim)
        # Categorical dimensions are exact-match; distributions / numeric
        # we treat as different if the dominant key / bucket differs.
        if isinstance(va, dict) and isinstance(vb, dict):
            # Filter out None values; some LLM outputs include nulls
            # for missing dimensions and max() can't compare None to str.
            clean_a = {k: v for k, v in va.items() if v is not None}
            clean_b = {k: v for k, v in vb.items() if v is not None}
            top_a = max(clean_a.items(), key=lambda kv: kv[1])[0] if clean_a else None
            top_b = max(clean_b.items(), key=lambda kv: kv[1])[0] if clean_b else None
            if top_a != top_b:
                score += 1
        elif va != vb:
            score += 1
    return score


# ─── LLM connection eval ──────────────────────────────────────────────────

_CROSSCUT_EVAL_PROMPT = """\
You are evaluating whether two journalism pieces would work as a
"Crosscut" — a paired-listening episode where two rigorous, thoughtful
articles approach the same question from different angles.

A good Crosscut pair has:
  - A SHARED QUESTION or topic that both articles are wrestling with,
    even if they don't say it the same way.
  - DIFFERENT ANGLES on that question — different methodologies, time
    horizons, scales, domains, or stances. They don't have to disagree.
  - INTELLECTUAL SUBSTANCE — pieces a thoughtful reader would gain from
    putting in conversation. Not just both-pieces-mention-X.

A BAD pair has:
  - No real shared question, just incidental topical overlap
  - The same angle approached the same way (would be redundant audio)
  - One piece is a hot take, gossip, or substantively thin

Score the pair from 0 (terrible) to 10 (genuinely illuminating).

Use the FULL range — most pairs are not 9/10. Calibration anchors:

  10 — Once-a-month pairing. You would actively recommend this episode
       to a thoughtful friend. The two pieces are clearly on the same
       question AND illuminate each other in a way neither does alone.
       The connection is surprising or non-obvious.
   8 — Strong pair. Same question, real angle difference, listener
       comes away with a sharper view. Not surprising but solid.
   6 — Workable pair. Topically adjacent, somewhat different angles,
       but the connection feels stretched OR the angle difference is
       small. Episode would be okay, not memorable.
   4 — Weak pair. Topical overlap is incidental ("both pieces mention
       AI") rather than substantive shared question.
   2 — Bad pair. The two pieces aren't really on the same question.

Most candidate pairs the upstream stage hands you will be in the 4-7
range. Reserve 8-10 for pairs you would genuinely want to listen to.
Default to lower scores when uncertain — being too generous makes the
whole longlist look uniformly excellent and useless to a reviewer.

═══════════════════════════════════════════════════════════════════════
PIECE A — {{ pub_a }}
Title: {{ title_a }}
{{ excerpt_a }}

═══════════════════════════════════════════════════════════════════════
PIECE B — {{ pub_b }}
Title: {{ title_b }}
{{ excerpt_b }}

═══════════════════════════════════════════════════════════════════════
OUTPUT — a single JSON object with these exact keys:

{
  "shared_question": "<one short sentence naming the question both pieces engage>",
  "angle_a":         "<one short sentence naming A's angle / starting point>",
  "angle_b":         "<one short sentence naming B's angle / starting point>",
  "topic_label":     "<2-4 word topic for episode title, lowercase, no punctuation>",
  "connection_summary": "<one editorial sentence — what's interesting about putting these together>",
  "score": <integer 0-10>
}

topic_label: this becomes the episode's title (shown on /crosscuts,
the episode page, and /listener-created), so it has to mean something
to a listener who hasn't read either article — not an internal
shorthand. Plain, concrete words a smart generalist would use, not a
category label. "iran war new angles" not "geopolitical reframing";
"remembering pictures" not "visual memory retention".

Aarva voice for connection_summary: reflective, curious, open-minded,
third-person (never "I", "we", "us", "our" — Aarva is a curatorial
voice, not a personality). Smart-generalist register per
docs/session_plan_content_quality.md §1 — concrete image over
abstract noun, plain verbs, no jargon. Use observations:
  - "What's interesting is that both writers reach for X, but for
     very different reasons."
  - "These two pieces share an assumption that's rarely questioned."
  - "Read together, the pieces surface a tension neither addresses
     head-on."
Avoid: "both writers raise important points", "this debate is critical
now more than ever", any pundit-style synthesis, any "I"/"we" framing,
and the forbidden words/phrases below.

[[HUMAN_VOICE]]
Output ONLY the JSON object. No preamble, no explanation, no markdown
code fence.
"""


def _render(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items():
        out = out.replace("{{ " + k + " }}", str(v))
    return out


def _eval_pair_via_llm(
    llm: LLMClient,
    a: _CrosscutArticle,
    b: _CrosscutArticle,
) -> Optional[dict]:
    """Ask Gemini to evaluate one pair. Returns the parsed JSON, or
    None on failure.

    Sends the FULL article body for each piece (capped at 25k chars
    ~= 5,000 words to bound input tokens). The earlier 1,500-char
    excerpt was too short for the model to judge angle differences
    accurately — many pieces don't reveal their actual stance until
    paragraph 4-5. The cap protects against runaway tokens on
    unusually long pieces; almost all articles fit comfortably under
    it.
    """
    _MAX_BODY = 25_000
    body_a = (a.full_text or a.excerpt or "")[:_MAX_BODY]
    body_b = (b.full_text or b.excerpt or "")[:_MAX_BODY]
    prompt = _render(
        _CROSSCUT_EVAL_PROMPT,
        pub_a=a.publication_name, title_a=a.title,
        excerpt_a=body_a,
        pub_b=b.publication_name, title_b=b.title,
        excerpt_b=body_b,
    )
    try:
        result = llm.complete(prompt, expect_json=True, temperature=0.4)
        if not isinstance(result, dict):
            return None
        # Coerce and validate the required keys.
        out = {
            "shared_question":     str(result.get("shared_question", "")).strip(),
            "angle_a":             str(result.get("angle_a", "")).strip(),
            "angle_b":             str(result.get("angle_b", "")).strip(),
            "topic_label":         str(result.get("topic_label", "")).strip().lower(),
            "connection_summary":  str(result.get("connection_summary", "")).strip(),
            "score":               int(result.get("score", 0)),
        }
        return out
    except Exception as e:
        logger.warning("Crosscut eval LLM call failed: %s", e)
        return None


# ─── Divergent-view tier (2026-07-15) ─────────────────────────────────────
#
# Layered ABOVE the connection-eval above, not a replacement for it. See
# docs/session_plan_users_and_crosscut_upgrades.md Section 2: prefer pairs
# that argue opposing sides of the same question over pairs that merely
# offer complementary angles, when such pairs exist in the pool. Editorial
# voice is unchanged either way — this only affects which pairs get
# selected, not how the eventual intro/bridge/outro are written.

_STANCE_CLASSIFY_PROMPT = """\
You classify pairs of journalism articles by their relationship.

Return ONE label:

- OPPOSING_VIEWS: The two articles argue different sides of the SAME
  question. Not just different angles or different subjects — the two
  authors would meaningfully disagree if they met. Example: one piece
  arguing carbon capture is essential to hit climate targets; another
  arguing it's a fossil-fuel-industry greenwash. Same question,
  opposing conclusions.
- DIFFERENT_ANGLES: The articles are about the same topic but come at
  it from complementary or non-overlapping angles — they'd nod at each
  other, not argue. Example: one piece on the economics of AI training;
  another on its cultural impact. Same topic, different lenses.

When in doubt, prefer DIFFERENT_ANGLES — only label OPPOSING_VIEWS when
the disagreement is clear and would survive the two authors meeting
in person.

═══════════════════════════════════════════════════════════════════════
PIECE A — {{ pub_a }}
Title: {{ title_a }}
{{ excerpt_a }}

═══════════════════════════════════════════════════════════════════════
PIECE B — {{ pub_b }}
Title: {{ title_b }}
{{ excerpt_b }}

═══════════════════════════════════════════════════════════════════════
OUTPUT — a single JSON object with exactly one key:

{"stance": "OPPOSING_VIEWS" | "DIFFERENT_ANGLES"}

Output ONLY the JSON object. No prose, no markdown code fence.
"""


def _classify_pair_stance(
    llm: LLMClient,
    a: _CrosscutArticle,
    b: _CrosscutArticle,
) -> str:
    """Classify whether a pair argues opposing views on the same
    question, or merely offers different angles on the same topic.

    Defaults to DIFFERENT_ANGLES on any parse error or unexpected
    response — the safer fallback, since that's the tier that already
    works well today; a misfire here should silently degrade to
    today's behavior, not wrongly promote a weak pair into the
    divergent tier.

    Excerpt clipped to 1,500 chars (vs. _eval_pair_via_llm's 25k) —
    enough to convey an argument's shape for a binary classification,
    not the full nuance a quality/rationale judgment needs."""
    _MAX_EXCERPT = 1500
    excerpt_a = (a.full_text or a.excerpt or "")[:_MAX_EXCERPT]
    excerpt_b = (b.full_text or b.excerpt or "")[:_MAX_EXCERPT]
    prompt = _render(
        _STANCE_CLASSIFY_PROMPT,
        pub_a=a.publication_name, title_a=a.title, excerpt_a=excerpt_a,
        pub_b=b.publication_name, title_b=b.title, excerpt_b=excerpt_b,
    )
    try:
        result = llm.complete(prompt, expect_json=True, temperature=0.2)
        if not isinstance(result, dict):
            return "DIFFERENT_ANGLES"
        stance = str(result.get("stance", "")).strip().upper()
        return "OPPOSING_VIEWS" if stance == "OPPOSING_VIEWS" else "DIFFERENT_ANGLES"
    except Exception as e:
        logger.warning("Crosscut stance classification failed: %s", e)
        return "DIFFERENT_ANGLES"


# ─── Persistence ──────────────────────────────────────────────────────────

def _clear_today_candidates(db: Database, today: date) -> int:
    """SOFT-supersede today's unbuilt candidates instead of deleting.

    Previously we DELETE'd, but that wiped the "seen articles" history
    that --require-fresh relies on. Now we set superseded_at on each
    row so the longlist CLI sees only the current run's candidates,
    but the seen-history queries can still find old article_ids in
    the table.

    Built candidates (edition_id IS NOT NULL) are left untouched.
    """
    with db.connect() as conn:
        cur = conn.execute("""
            UPDATE crosscut_pair_candidates
               SET superseded_at = CURRENT_TIMESTAMP
             WHERE candidate_date = ?
               AND edition_id IS NULL
               AND superseded_at IS NULL
        """, (today.isoformat(),))
        return cur.rowcount


def _persist_candidate(
    db: Database,
    today: date,
    a: _CrosscutArticle,
    b: _CrosscutArticle,
    eval_out: dict,
    divergence: float,
    stance: Optional[str] = None,
) -> int:
    with db.connect() as conn:
        cur = conn.execute("""
            INSERT INTO crosscut_pair_candidates
                (candidate_date, article_a_id, article_b_id,
                 topic_label, angle_a_label, angle_b_label,
                 connection_summary, connection_score, divergence_score,
                 stance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (today.isoformat(), a.id, b.id,
              eval_out.get("topic_label"),
              eval_out.get("angle_a"),
              eval_out.get("angle_b"),
              eval_out.get("connection_summary"),
              float(eval_out.get("score") or 0),
              float(divergence),
              stance))
        return int(cur.lastrowid)


# ─── Phase 3 — Episode script generation ──────────────────────────────────
#
# After the user has selected one pair from the longlist via aarva.crosscut,
# the script-generation pass produces:
#   - intro_text  (~100 words) framing the topic + the two angles
#   - bridge_text per piece (the connective commentary that precedes that
#     piece in the audio; first piece's bridge is the article-intro,
#     second piece's bridge is the cross-piece connective)
#   - outro_text (~80 words) landing the takeaway
#   - key passages per article (2-3 paragraphs that load-bear the angle)
#
# Voice direction: third-person curatorial voice — reflective, curious,
# open, never first-person (see AGENTS.md rule 9a). Smart-generalist
# register per docs/session_plan_content_quality.md §1 — this stale
# comment previously said "first-person Aarva voice", which the actual
# prompts below have always contradicted.


_HUMAN_VOICE_RULES = """\
═══════════════════════════════════════════════════════════════════════
HUMAN VOICE — avoid AI tells (most important rule)
═══════════════════════════════════════════════════════════════════════

The narration must sound like a thoughtful human essayist, not an LLM.
A listener should never be able to spot a tell.

FORBIDDEN WORDS (these are dead giveaways):
  delve, delves, delving; tapestry; navigate / navigating (as metaphor);
  realm; underscores / underscoring; highlights (as verb); showcases;
  intricate; intricacies; myriad; robust; leveraging; fascinating;
  crucial; pivotal (as filler); landscape (as metaphor); embark;
  unpack / unpacking (as a synonym for explain); resonates with;
  lies at the heart of; multifaceted; holistic; ever-evolving;
  paramount; testament to; speaks volumes; resonance; juxtaposition;
  interrogates; grapples with; the discourse; the fabric of; the
  essence of; what it means to be.

FORBIDDEN PHRASES:
  "in the realm of"; "in today's world"; "at its core"; "in essence";
  "it's important to note"; "let's explore"; "deep dive"; "rich
  tapestry"; "complex interplay"; "delicate balance"; "in a world
  where"; "raises important questions"; "now more than ever";
  "stands as a testament"; "the world of [X]".

PATTERNS TO AVOID:
  — Triadic lists as default rhythm ("X, Y, and Z" stacked repeatedly).
  — The "not X, but Y" rhetorical pattern leaned on more than once.
  — Em-dash overuse — one or two per paragraph is fine; four is LLM.
  — "Moreover" / "furthermore" as transition crutches.
  — Vague meta-commentary that says nothing concrete about the piece.
  — Opening with the topic noun-phrase as the subject ("Migration is
    one of the defining questions of our time"). Start with something
    specific from the article instead.
  — Sentences that start "This piece / this episode examines / argues
    / traces / grapples with…". Cut the construction — use an active
    verb a person would actually say.

═══════════════════════════════════════════════════════════════════════
TARGET READER (docs/session_plan_content_quality.md §1 — locked, all
listener-facing copy targets this standard)
═══════════════════════════════════════════════════════════════════════

Any smart generalist, including someone who isn't a college graduate —
not someone who reads philosophy for fun. Think J.K. Rowling writing
for adults, not Salman Rushdie or V.S. Naipaul: warm, specific, easy
to follow, occasionally pointed. No philosophical density, no
hedging, no abstract nouns where a concrete image works.

Before finalizing, check against every one of these:
  — Would this land with a curious 18-year-old with no college
    background? If not, simplify.
  — Read it aloud in one breath — does any sentence trip? Break it.
  — Is there a concrete image in the first sentence — a person, a
    place, an object, an act? If it opens on an abstract noun
    ("resonance", "framework", "phenomenon"), rewrite.
  — Are there words a 12-year-old wouldn't know? Swap them or
    introduce them naturally.

WRITE THE WAY AN ESSAYIST WRITES FOR THE EAR:
  Contractions ("it's", "doesn't"). Short sentences mixed with longer
  ones. Specific nouns over abstract ones. Plain verbs. Lift concrete
  details from the actual article — names, places, numbers, a vivid
  image — rather than abstracting about what the article does.
"""


_INTRO_PROMPT = """\
Write the OPENING for a Crosscut episode — Aarva's paired-listening
format where two rigorous articles approach the same question from
different angles.

The opening names the topic and previews the two angles in Aarva's
editorial voice. The listener should know what they're about to
encounter and why putting these two together is interesting.

═══════════════════════════════════════════════════════════════════════
LENGTH & VOICE
═══════════════════════════════════════════════════════════════════════

— 90–120 words. One paragraph.
— Reflective-essay register: empathetic, curious, open, with a sense
  of surprising discovery. Like a thoughtful radio essay narrator.
— **NEVER use first person ("I", "me", "my", "we", "us", "our").**
  Aarva is a curatorial voice, not a personality. Use observations:
  "What's striking is…", "Something interesting emerges when…",
  "There's a real puzzle in…", "It's worth pausing on…".
— No spoilers — set up the question, don't pre-answer it.
— **Surface WHEN each piece was published if it adds context** —
  especially when one piece is older than the other, or when the
  date matters for how the argument should be read (e.g., a pre-
  election essay vs. a post-election one). Use the `published_date_a`,
  `published_date_b`, and `today` fields to weave dates in naturally:
  "writing in March…", "a 2021 piece that…", "from earlier this year".
  If both pieces are similarly recent and the date doesn't add much,
  skip — don't force it.
{{ prompt_acknowledgment }}
═══════════════════════════════════════════════════════════════════════
DON'T
═══════════════════════════════════════════════════════════════════════

— Don't say "today's episode" or "in this episode"
— Don't say "we'll hear from" — Aarva is one voice, not a panel
— Don't use first-person at all (no "I", "we", "us", "our")
— Don't recap either article in detail
— Don't claim the topic is "more important than ever"
— Don't open with "Have you ever wondered…"

[[HUMAN_VOICE]]
═══════════════════════════════════════════════════════════════════════
NAMING RULE
═══════════════════════════════════════════════════════════════════════

If you name a writer, use the byline EXACTLY as given. Do NOT add
titles, do NOT shorten or modify, do NOT invent names. If a byline is
Unknown, refer to "the writer" or "the piece" instead.

═══════════════════════════════════════════════════════════════════════
PAIR
═══════════════════════════════════════════════════════════════════════

Topic:             {{ topic_label }}
Shared question:   {{ shared_question }}
Today's date:      {{ today }}
Angle A:           {{ angle_a }}  ({{ pub_a }} — "{{ title_a }}", by {{ byline_a }}, published {{ published_date_a }})
Angle B:           {{ angle_b }}  ({{ pub_b }} — "{{ title_b }}", by {{ byline_b }}, published {{ published_date_b }})
Connection:        {{ connection_summary }}

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════

Output just the paragraph. No preamble. No quotation marks. No labels.
"""


_SUBHEAD_HOOK_PROMPT = """\
Write the SUB-HEADING HOOK for a Crosscut episode — content-quality
Section 2 (docs/session_plan_content_quality.md §2). This is a single
listener-facing sentence shown under the episode's topic-label title
on the browse page and the episode page, replacing what used to be a
plain "Title A × Title B" byline.

═══════════════════════════════════════════════════════════════════════
LENGTH & VOICE
═══════════════════════════════════════════════════════════════════════

— One sentence, 20–40 words.
— Poses a listener question, or names the shared insight the pairing
  offers. Do NOT restate either article's title.
— **NEVER use first person** ("I", "we", "us", "our").
{{ prompt_acknowledgment }}
[[HUMAN_VOICE]]
═══════════════════════════════════════════════════════════════════════
PAIR
═══════════════════════════════════════════════════════════════════════

Topic:            {{ topic_label }}
Shared question:  {{ shared_question }}
Angle A:           {{ angle_a }}
Angle B:           {{ angle_b }}
Connection:        {{ connection_summary }}

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════

Output just the one sentence. No preamble. No quotation marks. No labels.
"""


_BRIDGE_PROMPT_A = """\
Write the SHORT INTRO that immediately precedes the FIRST article in an
Aarva Crosscut episode.

This bridge is the listener's handoff into the first piece. It names
the writer (using the EXACT byline below — do not invent or alter
names), names the angle they're bringing, and gives the listener one
specific thing to notice as they listen — something concrete from the
piece, not a generic frame.

═══════════════════════════════════════════════════════════════════════
LENGTH & VOICE
═══════════════════════════════════════════════════════════════════════

— 50–80 words.
— Reflective-essay register: empathetic, curious, conversational.
  Not announcer voice.
— **NEVER use first person.** No "I", "me", "we", "us". Use
  observations: "Notice how she…", "What's worth listening for is…",
  "The pivotal move comes when…".
— Point at a SPECIFIC argument or move the writer makes — not a
  generic summary.
— **Use the byline exactly as given. Do NOT add titles like "Dr."
  unless the byline includes them. Do NOT shorten or modify the name.
  If the byline is multiple authors, refer to them as "the authors"
  or include all names. If the byline is empty/Unknown, say "the
  writer" or "the piece" — never invent a name.**
— **Surface WHEN the piece was published.** Especially for politics,
  tech, and current-affairs pieces, the date matters for how a
  listener interprets the argument. Weave it in naturally — don't
  read out an ISO date. Use the given `published_date_a` and `today`
  fields to pick relative phrasings: "writing this March", "in a
  late-2025 essay", "back in February", "from earlier this year",
  "published last week", "a 2021 essay that…".

[[HUMAN_VOICE]]
═══════════════════════════════════════════════════════════════════════
ARTICLE A
═══════════════════════════════════════════════════════════════════════

Publication:    {{ pub_a }}
Byline:         {{ byline_a }}
Title:          {{ title_a }}
Published:      {{ published_date_a }}
Today's date:   {{ today }}
Angle:          {{ angle_a }}

{{ excerpt_a }}

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════

Output just the paragraph. No preamble. No quotation marks. No labels.
"""


_BRIDGE_PROMPT_BETWEEN = """\
Write the CROSS-PIECE BRIDGE in an Aarva Crosscut episode. This is the
paragraph the listener hears AFTER the first article ends and BEFORE
the second article begins.

This is the editorially richest moment in the episode. Its job is to:
  - Name what the first piece leaves the listener with
  - Surface what's interesting about turning the page to the second
    piece — what assumption it shares, where it diverges, what
    specific argument it'll challenge or complement
  - Frame as observation and discovery — what surfaces from putting
    these two together — not as adjudication

═══════════════════════════════════════════════════════════════════════
LENGTH & VOICE
═══════════════════════════════════════════════════════════════════════

— 150–220 words.
— Reflective-essay register: empathetic, open-minded, with a sense
  of surprising discovery.
— **NEVER use first person.** No "I", "me", "we", "us", "our".
  Use observations and the second-person/third-person:
  "What jumped out…", "Notice how…", "The interesting thing is…",
  "Read alongside Mendez, Park's caution starts to look like…"
— Point at SPECIFIC arguments — not "the other side has valid points"
  or "they raise important questions."

═══════════════════════════════════════════════════════════════════════
GOOD EXAMPLES (tone & shape)
═══════════════════════════════════════════════════════════════════════

— "What jumps out reading Smith is the assumption that X is even the
   right frame. Roberts, up next, never says it explicitly, but her
   piece basically takes the opposite starting point — and she still
   ends up worried about the same outcome. The two writers would
   almost certainly disagree about Y, but the shared concern about Z
   is harder to dismiss."

— "Where Park leaves the question open, Mendez closes it — and the
   way she closes it sends you back to re-read Park. He reads cautious
   on the first pass; alongside Mendez, his caution looks more like
   honesty about uncertainty."

═══════════════════════════════════════════════════════════════════════
DON'T
═══════════════════════════════════════════════════════════════════════

— Don't use first person ("I", "we", "me", "us", "our")
— Don't synthesise or adjudicate ("both pieces ultimately agree…")
— Don't pundit-summarise ("Smith argues X while Roberts argues Y")
— Don't moralise
— Don't repeat the angles from the intro verbatim

[[HUMAN_VOICE]]
═══════════════════════════════════════════════════════════════════════
NAMING RULE
═══════════════════════════════════════════════════════════════════════

Use the bylines EXACTLY as given. Do NOT add titles like "Dr." unless
the byline includes them. Do NOT shorten or modify a name. If a byline
is multiple authors, use all names or "the authors". If a byline is
empty/Unknown, say "the writer" or "the piece" — never invent a name.

═══════════════════════════════════════════════════════════════════════
PAIR
═══════════════════════════════════════════════════════════════════════

Topic:             {{ topic_label }}
Shared question:   {{ shared_question }}

ARTICLE A — {{ pub_a }} — "{{ title_a }}"
Byline:  {{ byline_a }}
Angle:   {{ angle_a }}
{{ excerpt_a }}

ARTICLE B — {{ pub_b }} — "{{ title_b }}"
Byline:  {{ byline_b }}
Angle:   {{ angle_b }}
{{ excerpt_b }}

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════

Output just the paragraph. No preamble. No quotation marks. No labels.
"""


_OUTRO_PROMPT = """\
Write the CLOSING for a Crosscut episode. The listener has just heard
both articles + the bridge between them. The outro lands a takeaway —
what the listener might carry away from the pairing — and offers one
honest, open question rather than a tidy conclusion.

═══════════════════════════════════════════════════════════════════════
LENGTH & VOICE
═══════════════════════════════════════════════════════════════════════

— 70–100 words.
— Reflective-essay register: empathetic, humble, with a sense of
  open inquiry. Like the end of a good radio essay.
— **NEVER use first person.** No "I", "we", "me", "us", "our".
  Use observations: "What lingers is…", "What's left to sit with…",
  "The question these two pieces leave behind is…"
— End on a real open question, not a rhetorical flourish.
— Don't repeat the intro. Don't summarise either piece.

[[HUMAN_VOICE]]
═══════════════════════════════════════════════════════════════════════
PAIR
═══════════════════════════════════════════════════════════════════════

Topic:           {{ topic_label }}
Shared question: {{ shared_question }}
Angle A:         {{ angle_a }}
Angle B:         {{ angle_b }}
Connection:      {{ connection_summary }}

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════

Output just the paragraph. No preamble. No quotation marks. No labels.
"""


# Inject the shared anti-LLM-language rules into each of the six
# editorial prompts. Done at module load so the rendered prompt is one
# string; the rules block can be edited in one place (above) and every
# section voice automatically picks it up.
_INTRO_PROMPT           = _INTRO_PROMPT.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)
_BRIDGE_PROMPT_A        = _BRIDGE_PROMPT_A.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)
_BRIDGE_PROMPT_BETWEEN  = _BRIDGE_PROMPT_BETWEEN.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)
_OUTRO_PROMPT           = _OUTRO_PROMPT.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)
_CROSSCUT_EVAL_PROMPT   = _CROSSCUT_EVAL_PROMPT.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)
_SUBHEAD_HOOK_PROMPT    = _SUBHEAD_HOOK_PROMPT.replace("[[HUMAN_VOICE]]", _HUMAN_VOICE_RULES)


@dataclass
class CrosscutBuildStats:
    edition_id: Optional[int] = None
    candidate_id: Optional[int] = None
    intro_generated: bool = False
    bridges_generated: int = 0
    outro_generated: bool = False
    passages_loaded: int = 0    # how many of the two articles had non-empty bodies
    errors: int = 0


def _selected_candidate(db: Database, today: date) -> Optional[dict]:
    """Return the most recently selected candidate for today, or None."""
    with db.connect() as conn:
        row = conn.execute("""
            SELECT cpc.*,
                   a.title AS title_a, a.full_text AS full_text_a, a.byline AS byline_a,
                   a.word_count AS wc_a,
                   a.published_date AS published_date_a,
                   pa.name AS pub_a,
                   b.title AS title_b, b.full_text AS full_text_b, b.byline AS byline_b,
                   b.word_count AS wc_b,
                   b.published_date AS published_date_b,
                   pb.name AS pub_b,
                   sa.fingerprint_json AS fp_a,
                   sb.fingerprint_json AS fp_b
              FROM crosscut_pair_candidates cpc
              JOIN articles a  ON a.id  = cpc.article_a_id
              JOIN articles b  ON b.id  = cpc.article_b_id
              JOIN publications pa ON pa.id = a.publication_id
              JOIN publications pb ON pb.id = b.publication_id
              LEFT JOIN article_scores sa ON sa.article_id = a.id
              LEFT JOIN article_scores sb ON sb.article_id = b.id
             WHERE cpc.candidate_date = ?
               AND cpc.selected_at IS NOT NULL
             ORDER BY cpc.selected_at DESC
             LIMIT 1
        """, (today.isoformat(),)).fetchone()
    return dict(row) if row else None


def _shared_question_for(cand: dict) -> str:
    """We didn't store the shared_question separately; reconstruct it
    from the connection_summary which mentions it. Fall back to the
    topic label as a last resort."""
    return (cand.get("connection_summary") or "").split(".")[0] or cand.get("topic_label", "")


def _generate_text(
    llm: LLMClient,
    template: str,
    *,
    expect_json: bool = False,
    temperature: float = 0.6,
    **kwargs,
) -> Optional[str | dict]:
    """Render a prompt template and ask Gemini for the output. Returns
    the string (or dict if expect_json) or None on failure."""
    prompt = _render(template, **kwargs)
    try:
        result = llm.complete(prompt, expect_json=expect_json, temperature=temperature)
        return result
    except Exception as e:
        logger.warning("Crosscut script-gen LLM call failed: %s", e)
        return None


def _persist_episode(
    db: Database,
    today: date,
    cand: dict,
    intro: str,
    bridge_a: str,
    bridge_between: str,
    outro: str,
    passage_a: str,
    passage_b: str,
    *,
    target_db: Optional[Database] = None,
    subhead_hook: Optional[str] = None,
    originating_prompt: Optional[str] = None,
) -> int:
    """Create the editions row + 2 edition_pieces rows for the crosscut
    episode. Returns the new edition_id. Both pieces start at
    review_status='proposed' so the user's Phase 6 review can catch
    anything that misses the mark.

    target_db: when set (the on-demand /create flow), the editions +
    edition_pieces rows are written here instead of `db` — see
    aarva/listener_db.py. That file has no `articles` table, so
    edition_pieces also gets the article title/publication/byline
    denormalized from `cand` (already joined from the main DB by
    `_selected_candidate`) instead of relying on a join. `articles`
    status updates always go against `db` (the main DB) regardless,
    since `articles` never moves — and the candidate-link update is
    skipped for target_db writes, since crosscut_pair_candidates.edition_id
    would otherwise point at an id in the wrong database's `editions`
    table (nothing reads that link for the on-demand flow anyway).

    subhead_hook / originating_prompt: content-quality Sections 2/3 —
    see docs/session_plan_content_quality.md. Both NULL for daily-
    pipeline crosscuts."""
    write_db = target_db if target_db is not None else db
    with write_db.connect() as conn:
        # Editions row.
        cur = conn.execute("""
            INSERT INTO editions
                (edition_date, edition_type, topic_label, intro_text,
                 outro_text, subhead_hook, originating_prompt)
            VALUES (?, 'crosscut', ?, ?, ?, ?, ?)
        """, (today.isoformat(), cand.get("topic_label"), intro, outro,
              subhead_hook, originating_prompt))
        edition_id = int(cur.lastrowid)

        # Article A — first piece. bridge_text is the article-intro
        # ("here's how she puts it") that precedes the read-aloud
        # passage in the audio. The passage lives in the existing
        # show_notes column, repurposed for crosscut to hold the
        # read-aloud passage. Stage 9 reads show_notes for crosscut
        # pieces and treats it as the article body to narrate.
        if target_db is not None:
            conn.execute("""
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position,
                     bridge_text, show_notes, review_status,
                     article_title, article_publication, article_byline)
                VALUES (?, ?, 'crosscut_piece_a', 0, ?, ?, 'proposed', ?, ?, ?)
            """, (edition_id, int(cand["article_a_id"]), bridge_a, passage_a,
                  cand.get("title_a"), cand.get("pub_a"), cand.get("byline_a")))
            conn.execute("""
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position,
                     bridge_text, show_notes, review_status,
                     article_title, article_publication, article_byline)
                VALUES (?, ?, 'crosscut_piece_b', 1, ?, ?, 'proposed', ?, ?, ?)
            """, (edition_id, int(cand["article_b_id"]), bridge_between, passage_b,
                  cand.get("title_b"), cand.get("pub_b"), cand.get("byline_b")))
        else:
            conn.execute("""
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position,
                     bridge_text, show_notes, review_status)
                VALUES (?, ?, 'crosscut_piece_a', 0, ?, ?, 'proposed')
            """, (edition_id, int(cand["article_a_id"]), bridge_a, passage_a))
            conn.execute("""
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position,
                     bridge_text, show_notes, review_status)
                VALUES (?, ?, 'crosscut_piece_b', 1, ?, ?, 'proposed')
            """, (edition_id, int(cand["article_b_id"]), bridge_between, passage_b))

    # articles.status always lives in the main DB, regardless of which
    # DB the edition itself landed in.
    with db.connect() as conn:
        conn.execute(
            "UPDATE articles SET status = 'in_edition' WHERE id = ?",
            (int(cand["article_a_id"]),),
        )
        conn.execute(
            "UPDATE articles SET status = 'in_edition' WHERE id = ?",
            (int(cand["article_b_id"]),),
        )
        if target_db is None:
            # Link the candidate to the built edition. Only meaningful
            # when the edition landed in the same DB as the candidate
            # row (the daily pipeline flow) — see target_db docstring.
            conn.execute(
                "UPDATE crosscut_pair_candidates SET edition_id = ? WHERE id = ?",
                (edition_id, int(cand["id"])),
            )

    return edition_id


def build_episode_script(
    config: PipelineConfig,
    db: Database,
    *,
    llm: Optional[LLMClient] = None,
    target_db: Optional[Database] = None,
    originating_prompt: Optional[str] = None,
) -> CrosscutBuildStats:
    """Generate intro / bridge / outro / key passages for today's
    user-selected crosscut pair and persist the episode.

    llm: pass an existing client to avoid rebuilding (DI).
    target_db: passed straight through to `_persist_episode` — set by
    the on-demand /create worker to route the built episode into the
    listener DB instead of the main DB.
    originating_prompt: content-quality Section 3 — the listener's
    /create search string, when this build came from an on-demand
    request. None for daily-pipeline crosscuts. When set, the intro
    and subhead_hook prompts are told to acknowledge it.
    """
    stats = CrosscutBuildStats()
    today = date.today()

    cand = _selected_candidate(db, today)
    if not cand:
        logger.warning("Crosscut build: no selected pair for today. Run "
                       "`python -m aarva.crosscut` first to pick one.")
        return stats
    stats.candidate_id = int(cand["id"])

    # Check if we already built an edition for this candidate.
    if cand.get("edition_id"):
        logger.warning(
            "Crosscut build: candidate %d already built as edition %d. "
            "To rebuild, delete the existing edition first.",
            stats.candidate_id, int(cand["edition_id"]),
        )
        return stats

    if llm is None:
        llm = build_llm_client(config.llm)

    excerpt_a = (cand["full_text_a"] or "")[:3000]
    excerpt_b = (cand["full_text_b"] or "")[:3000]
    shared_q = _shared_question_for(cand)

    # Today's date is passed in so the LLM can render relative phrasings
    # ("last spring", "earlier this year") accurately when mentioning
    # an article's publication date.
    today_iso = today.isoformat()
    common_kwargs = dict(
        topic_label=cand.get("topic_label") or "",
        shared_question=shared_q,
        angle_a=cand.get("angle_a_label") or "",
        angle_b=cand.get("angle_b_label") or "",
        connection_summary=cand.get("connection_summary") or "",
        pub_a=cand.get("pub_a") or "",
        title_a=cand.get("title_a") or "",
        byline_a=cand.get("byline_a") or "Unknown",
        published_date_a=str(cand.get("published_date_a") or "Unknown"),
        pub_b=cand.get("pub_b") or "",
        title_b=cand.get("title_b") or "",
        byline_b=cand.get("byline_b") or "Unknown",
        published_date_b=str(cand.get("published_date_b") or "Unknown"),
        today=today_iso,
        excerpt_a=excerpt_a,
        excerpt_b=excerpt_b,
    )

    # Content-quality Section 3: when this build came from a listener's
    # /create search, tell the intro + subhead_hook prompts to
    # acknowledge it. Computed once here (rather than baked into
    # common_kwargs) since the intro and subhead prompts each need
    # their own phrasing of the same instruction.
    intro_prompt_ack = ""
    subhead_prompt_ack = ""
    if originating_prompt:
        intro_prompt_ack = (
            "\n— **This episode was built from a listener's search: "
            f'"{originating_prompt}"**. The OPENING SENTENCE should '
            "acknowledge what they asked about before introducing the "
            "pairing — e.g. \"You asked about X — here are two pieces "
            "that come at it from different angles.\" Paraphrase "
            "naturally rather than quoting the search verbatim if it "
            "reads awkwardly aloud; keep the frame, not the exact words.\n"
        )
        subhead_prompt_ack = (
            "\n═══════════════════════════════════════════════════════"
            f'════════════\nLISTENER\'S SEARCH\n'
            "═══════════════════════════════════════════════════════"
            "════════════\n\n"
            f'This episode was built from a listener\'s search: "{originating_prompt}"\n'
            "Tie the hook back to what they asked about — it should "
            "read as engaging their question, not just describing the "
            "pairing in the abstract.\n"
        )

    logger.info("Crosscut build: generating intro…")
    intro = _generate_text(
        llm, _INTRO_PROMPT, temperature=0.7,
        prompt_acknowledgment=intro_prompt_ack, **common_kwargs,
    )
    if intro:
        stats.intro_generated = True

    logger.info("Crosscut build: generating sub-heading hook…")
    subhead_hook = _generate_text(
        llm, _SUBHEAD_HOOK_PROMPT, temperature=0.6,
        prompt_acknowledgment=subhead_prompt_ack, **common_kwargs,
    )
    if isinstance(subhead_hook, str):
        subhead_hook = subhead_hook.strip()

    logger.info("Crosscut build: generating bridge A (intro to piece A)…")
    bridge_a = _generate_text(
        llm, _BRIDGE_PROMPT_A, temperature=0.6,
        pub_a=common_kwargs["pub_a"], title_a=common_kwargs["title_a"],
        byline_a=common_kwargs["byline_a"],
        published_date_a=common_kwargs["published_date_a"],
        today=common_kwargs["today"],
        angle_a=common_kwargs["angle_a"], excerpt_a=excerpt_a,
    )
    if bridge_a:
        stats.bridges_generated += 1

    logger.info("Crosscut build: generating cross-piece bridge…")
    bridge_between = _generate_text(
        llm, _BRIDGE_PROMPT_BETWEEN, temperature=0.6, **common_kwargs,
    )
    if bridge_between:
        stats.bridges_generated += 1

    logger.info("Crosscut build: generating outro…")
    outro = _generate_text(llm, _OUTRO_PROMPT, temperature=0.6, **common_kwargs)
    if outro:
        stats.outro_generated = True

    # Listeners hear the full article body, not an LLM-picked excerpt.
    # Bridge_a serves as the editorial intro to article A; bridge_between
    # is the transition between A and B.
    passage_a = (cand["full_text_a"] or "").strip()
    passage_b = (cand["full_text_b"] or "").strip()
    if passage_a:
        stats.passages_loaded += 1
    if passage_b:
        stats.passages_loaded += 1
    logger.info(
        "Crosscut build: full article bodies loaded "
        "(A=%d chars, B=%d chars)",
        len(passage_a), len(passage_b),
    )
    # TTS chunks at ~2500 chars; very long articles produce many chunks
    # each with its own retry budget, increasing the chance of one
    # chunk shipping as silence. Warn so the operator knows to spot-
    # check the audio output for long pieces.
    _LONG_PASSAGE_WARN = 30_000  # ~5000 words; ~12 chunks
    for name, body in (("A", passage_a), ("B", passage_b)):
        if len(body) > _LONG_PASSAGE_WARN:
            logger.warning(
                "Crosscut build: article %s is %d chars (~%d words) — "
                "TTS will chunk this into many pieces. Spot-check the "
                "final audio for mid-article drift or silence.",
                name, len(body), len(body) // 6,
            )

    if not all([intro, bridge_a, bridge_between, outro, passage_a, passage_b]):
        logger.error(
            "Crosscut build: incomplete script generation "
            "(intro=%s bridge_a=%s bridge_between=%s outro=%s "
            "passage_a=%d chars passage_b=%d chars). "
            "Not persisting the episode — re-run after fixing.",
            bool(intro), bool(bridge_a), bool(bridge_between), bool(outro),
            len(passage_a or ""), len(passage_b or ""),
        )
        stats.errors += 1
        return stats

    # subhead_hook is a display enhancement, not core content — a
    # failure here shouldn't block the episode (templates fall back
    # to "title_a x title_b" cleanly when it's NULL, per Section 2).
    if not subhead_hook:
        logger.warning(
            "Crosscut build: subhead_hook generation failed or empty — "
            "episode will fall back to 'title_a x title_b' display."
        )
        subhead_hook = None

    stats.edition_id = _persist_episode(
        db, today, cand,
        intro=intro, bridge_a=bridge_a, bridge_between=bridge_between,
        outro=outro, passage_a=passage_a, passage_b=passage_b,
        target_db=target_db,
        subhead_hook=subhead_hook, originating_prompt=originating_prompt,
    )
    logger.info(
        "Crosscut build: edition #%d created. Next step: review the "
        "script (intro/bridges/outro/passages) — coming in Phase 6 "
        "review CLI.",
        stats.edition_id,
    )

    # Embed the new episode into the search vector space. Both variants
    # (pairing_summary text + mean of source-article vectors) land in
    # crosscut_embeddings, tagged with the current embedding model so a
    # later model swap can be detected (and `article_mean` re-derived
    # by re-running scripts/backfill_crosscut_embeddings.py). Non-
    # blocking: a failure here doesn't taint the built episode — if
    # embedding fails the episode is still listenable and discoverable
    # via /editions and /crosscuts, just not via search until the
    # backfill script catches it up.
    try:
        from aarva.clients.embedding import build_embedding_client
        from aarva.services.crosscut_embeddings import embed_crosscut_episode
        emb_client = build_embedding_client(config.raw.get("embedding", {}))
        # target_db writes: the crosscut row we just built lives in
        # target_db (no articles table there), but the source articles'
        # own embeddings only exist in `db` (the main DB) — pass both
        # so embed_crosscut_episode reads/writes the right one of each.
        # Skip the extra DB round-trip: we already have every field
        # embed_crosscut_episode needs to build the pairing-summary
        # text sitting in local variables from the generation above.
        emb_stats = embed_crosscut_episode(
            target_db if target_db is not None else db,
            emb_client, stats.edition_id,
            crosscut={
                "topic_label": cand.get("topic_label"),
                "intro_text": intro,
                "bridge_between": bridge_between,
                "outro_text": outro,
                "article_a_id": int(cand["article_a_id"]),
                "article_b_id": int(cand["article_b_id"]),
            } if target_db is not None else None,
            articles_db=db,
        )
        logger.info(
            "Crosscut build: embedded edition #%d into %s space "
            "(pairing_summary=%d, article_mean=%d, errors=%d)",
            stats.edition_id, emb_client.name,
            emb_stats.pairing_embedded, emb_stats.article_mean_embedded,
            emb_stats.errors,
        )
    except Exception as e:
        logger.warning(
            "Crosscut build: search-index embedding failed for edition "
            "#%d: %s (non-blocking; run "
            "scripts/backfill_crosscut_embeddings.py to retry)",
            stats.edition_id, e,
        )

    return stats


# ─── Public entry point ───────────────────────────────────────────────────

def detect_pair_candidates(
    config: PipelineConfig,
    db: Database,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    sim_floor: float = DEFAULT_TOPICAL_SIM_FLOOR,
    sim_ceiling: float = DEFAULT_TOPICAL_SIM_CEILING,
    max_eval: int = DEFAULT_MAX_EVAL_CANDIDATES,
    longlist_size: int = DEFAULT_LONGLIST_SIZE,
    topic_recency_window: int = DEFAULT_TOPIC_RECENCY_WINDOW,
    require_fresh_article: bool = False,
    llm: Optional[LLMClient] = None,
) -> CrosscutPairStats:
    """Run pair detection for today. Persists the longlist to
    crosscut_pair_candidates. CLI reads from there for user review.

    llm: pass an existing client to avoid rebuilding (DI).
    """
    stats = CrosscutPairStats()
    today = date.today()

    pool = _load_crosscut_pool(db, lookback_days)
    stats.candidates_considered = len(pool)
    logger.info("Crosscut: %d scored articles from last %d days",
                len(pool), lookback_days)
    if len(pool) < 2:
        logger.warning("Crosscut: pool too small for pair detection")
        return stats

    recent_topics = _recent_crosscut_topics(db, topic_recency_window)
    if recent_topics:
        logger.info("Crosscut: skipping pairs whose topic_label matches "
                    "any of the last %d episodes' topics: %s",
                    topic_recency_window, sorted(recent_topics))

    selected_pairs = _previously_selected_pairs(
        db, DEFAULT_SELECTED_PAIR_EXCLUSION_DAYS,
    )
    if selected_pairs:
        logger.info("Crosscut: excluding %d previously-selected pairs "
                    "from the last %d days",
                    len(selected_pairs), DEFAULT_SELECTED_PAIR_EXCLUSION_DAYS)

    seen_articles: set[int] = set()
    if require_fresh_article:
        seen_articles = _previously_seen_article_ids(db)
        logger.info(
            "Crosscut: --require-fresh active. %d articles previously "
            "seen in past longlists; each new pair must include at "
            "least one article NOT in that set.",
            len(seen_articles),
        )

    # Structural pre-scoring: every (i, j) pair, score by topical similarity
    # band + divergence count.
    pre_scored: list[tuple[float, int, _CrosscutArticle, _CrosscutArticle]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a, b = pool[i], pool[j]
            if a.publication_name == b.publication_name:
                # Skip same-publication pairs — usually two pieces from
                # the same outlet share editorial worldview, defeating
                # the point of a crosscut.
                continue
            # Skip pairs we've already engaged with in a past run.
            if frozenset({a.id, b.id}) in selected_pairs:
                continue
            # If --require-fresh, at least one article must be new.
            if seen_articles and a.id in seen_articles and b.id in seen_articles:
                continue
            sim = _cosine(a.embedding, b.embedding)
            if sim < sim_floor or sim > sim_ceiling:
                continue
            div = _divergence(a, b)
            if div < 2:
                # Too structurally similar; even at moderate topic
                # similarity, this won't read as different angles.
                continue
            # Combined pre-score: rewards both being in the right
            # similarity band AND being structurally divergent.
            combined = div + (sim - sim_floor) * 2.0
            pre_scored.append((combined, div, a, b))
    stats.pairs_pre_scored = len(pre_scored)
    if not pre_scored:
        logger.warning("Crosscut: no pre-scored pair candidates found")
        return stats

    # Take top N by combined pre-score for LLM evaluation.
    pre_scored.sort(key=lambda r: r[0], reverse=True)
    to_eval = pre_scored[:max_eval]
    logger.info("Crosscut: %d pre-scored pairs, evaluating top %d via LLM",
                len(pre_scored), len(to_eval))

    if llm is None:
        llm = build_llm_client(config.llm)

    # Clear today's previous unpicked candidates so we don't double
    # up. Soft-supersede (not delete) so seen-history queries still see
    # them, and so the duplicate-pair filter in _previously_selected_
    # pairs still excludes any pair we've already shown the user today.
    superseded = _clear_today_candidates(db, today)
    if superseded:
        logger.info(
            "Crosscut: regenerating — superseded %d previously-shown "
            "candidate(s) from today. Pairs already shown will not "
            "reappear in this run.",
            superseded,
        )

    # Divergent-view tier (2026-07-15): classify each top-30 pair's
    # stance BEFORE the connection-eval so both buckets get evaluated
    # identically below. See docs/session_plan_users_and_crosscut_
    # upgrades.md Section 2 — this only changes which pairs get
    # selected, not how they're scored.
    divergent_evals: list[tuple[dict, _CrosscutArticle, _CrosscutArticle, float]] = []
    current_logic_evals: list[tuple[dict, _CrosscutArticle, _CrosscutArticle, float]] = []
    for combined, div, a, b in to_eval:
        stance = _classify_pair_stance(llm, a, b)
        stats.pairs_stance_classified += 1
        result = _eval_pair_via_llm(llm, a, b)
        stats.pairs_eval_called += 1
        if not result:
            continue
        # Topic-recency check (we have the LLM's topic_label now).
        topic = (result.get("topic_label") or "").strip().lower()
        if topic and topic in recent_topics:
            stats.skipped_for_topic_recency += 1
            logger.debug("Crosscut: skipping pair on recent topic '%s'", topic)
            continue
        if int(result.get("score") or 0) < 4:
            # Below quality floor; skip persisting low-scored pairs.
            continue
        entry = (result, a, b, float(div))
        if stance == "OPPOSING_VIEWS":
            divergent_evals.append(entry)
        else:
            current_logic_evals.append(entry)

    # Sort each bucket by LLM score (desc) independently — the stance
    # tag decides which bucket a pair is in; the quality score decides
    # its order within that bucket.
    divergent_evals.sort(key=lambda r: r[0].get("score") or 0, reverse=True)
    current_logic_evals.sort(key=lambda r: r[0].get("score") or 0, reverse=True)
    stats.pairs_divergent_found = len(divergent_evals)

    # Apply per-article appearance cap to diversify the longlist. Without
    # this, a few high-rigour articles dominate the longlist by pairing
    # with everything topically adjacent. We walk each bucket's sorted
    # list in priority order and admit pairs only while both articles
    # are under the appearance cap — the same `appearances` dict is
    # shared across both admit passes so an article picked in the
    # divergent tier can't also fill a current-logic slot.
    appearances: dict[int, int] = {}
    keep: list[tuple[dict, _CrosscutArticle, _CrosscutArticle, float, str]] = []

    def _admit(entries, quota: int, stance: str) -> int:
        admitted = 0
        for result, a, b, div in entries:
            if admitted >= quota:
                break
            if (appearances.get(a.id, 0) >= DEFAULT_MAX_APPEARANCES_PER_ARTICLE
                    or appearances.get(b.id, 0) >= DEFAULT_MAX_APPEARANCES_PER_ARTICLE):
                continue
            keep.append((result, a, b, div, stance))
            appearances[a.id] = appearances.get(a.id, 0) + 1
            appearances[b.id] = appearances.get(b.id, 0) + 1
            admitted += 1
        return admitted

    if divergent_evals:
        # 60/40 split in favour of divergent pairs. If fewer divergent
        # pairs exist than the 60% target, take all of them and fill
        # the rest from current-logic (never force a shortfall).
        target_divergent = round(longlist_size * 0.6)
        n_divergent = _admit(
            divergent_evals, min(target_divergent, len(divergent_evals)),
            "OPPOSING_VIEWS",
        )
        n_current = _admit(
            current_logic_evals, longlist_size - n_divergent,
            "DIFFERENT_ANGLES",
        )
        logger.info(
            "crosscut pair-select: divergent tier found %d pairs, "
            "filled longlist %d/%d (divergent/current-logic)",
            len(divergent_evals), n_divergent, n_current,
        )
    else:
        logger.info(
            "crosscut pair-select: no divergent pairs found, using "
            "current-logic only"
        )
        _admit(current_logic_evals, longlist_size, "DIFFERENT_ANGLES")

    for result, a, b, div, stance in keep:
        _persist_candidate(db, today, a, b, result, div, stance=stance)
        stats.pairs_persisted += 1

    logger.info(
        "Crosscut: persisted %d candidates (eval'd %d, stance-"
        "classified %d, skipped %d for topic recency, filtered <4 "
        "score)",
        stats.pairs_persisted, stats.pairs_eval_called,
        stats.pairs_stance_classified, stats.skipped_for_topic_recency,
    )
    return stats


# ─── Phase 4 — TTS assembly ───────────────────────────────────────────────
#
# Compose a single audio file for a crosscut episode by synthesizing six
# sections in order:
#
#   intro       (host voice)         — from editions.intro_text
#   bridge_a    (host voice)         — from edition_pieces[0].bridge_text
#   passage_a   (article-A voice)    — from edition_pieces[0].show_notes
#   bridge_btw  (host voice)         — from edition_pieces[1].bridge_text
#   passage_b   (article-B voice)    — from edition_pieces[1].show_notes
#   outro       (host voice)         — from editions.outro_text
#
# Three distinct voices give the listener clear auditory anchors: same
# host throughout, plus a distinct reader for each article. We stitch
# section WAVs together with a longer inter-section pause (~600ms) than
# the regular intra-chunk pause, so the structural transitions land.

# Default voice assignments. Configurable in pipeline.yaml under
# tts.crosscut_voices. Host = Sulafat (warm female) is the consistent
# Aarva-voice the listener gets to know across crosscut episodes;
# Charon and Vindemiatrix give clear male/female contrast for the two
# articles.
CROSSCUT_DEFAULT_HOST_VOICE       = "Sulafat"
CROSSCUT_DEFAULT_ARTICLE_A_VOICE  = "Charon"
CROSSCUT_DEFAULT_ARTICLE_B_VOICE  = "Vindemiatrix"

# Silence between major sections (intro → bridge → passage etc.) — bigger
# than the within-section inter-chunk pause so the structural beats are
# audible.
CROSSCUT_INTER_SECTION_PAUSE_MS = 600


@dataclass
class CrosscutTTSStats:
    edition_id: Optional[int] = None
    sections_synthesized: int = 0
    sections_skipped: int = 0
    total_audio_seconds: float = 0.0
    output_path: Optional[str] = None
    errors: int = 0


def _load_crosscut_edition_for_tts(db: Database, edition_id: int) -> Optional[dict]:
    """Pull the edition row + both pieces with their bridge and passage
    texts. Returns a dict suitable for the TTS assembly, or None if
    the edition isn't a built crosscut.

    Also resolves each piece's source publication_name, for the
    region-specific accent steer (2026-07-15 — docs/session_plan_
    users_and_crosscut_upgrades.md §3). The two DBs this runs against
    carry it differently: the listener DB denormalizes it onto
    edition_pieces.article_publication (no articles table there — see
    aarva/listener_db.py); the main DB doesn't carry it on
    edition_pieces at all, so this joins articles/publications
    directly there instead."""
    from aarva.listener_db import ListenerDatabase
    is_listener = isinstance(db, ListenerDatabase)
    with db.connect() as conn:
        e = conn.execute("""
            SELECT id, edition_date, edition_type, topic_label,
                   intro_text, outro_text
              FROM editions
             WHERE id = ?
               AND edition_type = 'crosscut'
        """, (edition_id,)).fetchone()
        if not e:
            return None
        if is_listener:
            pieces = conn.execute("""
                SELECT ep.article_id, ep.position, ep.slot,
                       ep.bridge_text, ep.show_notes AS passage,
                       ep.article_publication AS publication_name
                  FROM edition_pieces ep
                 WHERE ep.edition_id = ?
                 ORDER BY ep.position
            """, (edition_id,)).fetchall()
        else:
            pieces = conn.execute("""
                SELECT ep.article_id, ep.position, ep.slot,
                       ep.bridge_text, ep.show_notes AS passage,
                       p.name AS publication_name
                  FROM edition_pieces ep
                  JOIN articles a ON a.id = ep.article_id
                  JOIN publications p ON p.id = a.publication_id
                 WHERE ep.edition_id = ?
                 ORDER BY ep.position
            """, (edition_id,)).fetchall()
    if len(pieces) != 2:
        return None
    return {
        "edition": dict(e),
        "piece_a": dict(pieces[0]),
        "piece_b": dict(pieces[1]),
    }


def synthesize_crosscut_episode(
    config: PipelineConfig,
    db: Database,
    *,
    edition_id: Optional[int] = None,
    tts: Optional["TTSClient"] = None,
) -> CrosscutTTSStats:
    """Generate the audio file for a crosscut episode. If edition_id is
    omitted, picks the most recent built crosscut without audio yet.

    tts: pass an existing client to avoid rebuilding (DI).
    """
    import os
    import shutil
    import tempfile
    import wave
    from pathlib import Path as _Path

    from aarva.clients.tts import TTSClient, build_tts_client, _wav_duration

    stats = CrosscutTTSStats()

    if edition_id is None:
        with db.connect() as conn:
            row = conn.execute("""
                SELECT id FROM editions
                 WHERE edition_type = 'crosscut'
                 ORDER BY edition_date DESC, id DESC
                 LIMIT 1
            """).fetchone()
        if not row:
            logger.warning("Crosscut TTS: no crosscut editions found.")
            return stats
        edition_id = int(row["id"])

    payload = _load_crosscut_edition_for_tts(db, edition_id)
    if not payload:
        logger.warning("Crosscut TTS: edition %d is not a built crosscut "
                       "or doesn't have two pieces.", edition_id)
        return stats

    stats.edition_id = edition_id

    # Voice picks (configurable, defaults safe).
    tts_cfg = config.tts or {}
    crosscut_cfg = tts_cfg.get("crosscut_voices") or {}
    host_voice    = crosscut_cfg.get("host")    or CROSSCUT_DEFAULT_HOST_VOICE
    voice_a       = crosscut_cfg.get("article_a") or CROSSCUT_DEFAULT_ARTICLE_A_VOICE
    voice_b       = crosscut_cfg.get("article_b") or CROSSCUT_DEFAULT_ARTICLE_B_VOICE
    logger.info("Crosscut TTS: edition #%d — host=%s, article_a=%s, "
                "article_b=%s", edition_id, host_voice, voice_a, voice_b)

    # Build the section plan.
    e = payload["edition"]
    a = payload["piece_a"]
    b = payload["piece_b"]

    # Region-specific accent steer for the two article passages only
    # (2026-07-15 — docs/session_plan_users_and_crosscut_upgrades.md
    # §3). Reuses Stage 9's existing per-publication accent mechanism;
    # intro/bridges/outro stay in the neutral host voice with no
    # steer. None (no country tag on the publication) preserves
    # today's behavior exactly.
    from aarva.stages.stage_9_tts import (
        _accent_prompt_for, _build_publication_country_map,
    )
    country_map = _build_publication_country_map()
    accent_a = _accent_prompt_for(a, country_map)
    accent_b = _accent_prompt_for(b, country_map)
    if accent_a:
        logger.info("Crosscut TTS: piece_a accent: %s", accent_a)
    if accent_b:
        logger.info("Crosscut TTS: piece_b accent: %s", accent_b)

    sections: list[tuple[str, str, str, Optional[str]]] = [
        ("intro",       host_voice, e.get("intro_text") or "",   None),
        ("bridge_a",    host_voice, a.get("bridge_text") or "",  None),
        ("passage_a",   voice_a,    a.get("passage") or "",      accent_a),
        ("bridge_btw",  host_voice, b.get("bridge_text") or "",  None),
        ("passage_b",   voice_b,    b.get("passage") or "",      accent_b),
        ("outro",       host_voice, e.get("outro_text") or "",   None),
    ]
    sections = [(n, v, t.strip(), style) for n, v, t, style in sections if t.strip()]
    if not sections:
        logger.warning("Crosscut TTS: no narratable content in edition #%d",
                       edition_id)
        return stats

    # The Gemini TTS client is shared with the daily pipeline. We pass
    # the literal Gemini voice name (e.g., "Sulafat") via voice_id and
    # the client maps it through. Build once, reuse for all sections.
    if tts is None:
        tts = build_tts_client(tts_cfg)

    edition_date = date.fromisoformat(str(e["edition_date"]))
    audio_dir = config.audio_dir / edition_date.isoformat()
    audio_dir.mkdir(parents=True, exist_ok=True)

    # Per-section scratch dir. Lets a resumed build (after a Render
    # OOM mid-TTS) skip sections a prior attempt already finished
    # instead of re-synthesizing all 6 from scratch — see
    # docs/session_plan_worker_resumability.md's 2026-07-14 finding.
    # A section's WAV only lands here via an atomic move after
    # tts.synthesize fully succeeds, so a crash mid-synthesize can
    # never leave a partial file that gets mistaken for "already
    # done". Removed once the combined output is written below.
    scratch_dir = audio_dir / f"_tts_scratch_{edition_id}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Synthesize each section (or reuse one already on disk from a
    # prior attempt), then concatenate the PCM samples with
    # inter-section silence.
    section_pcms: list[bytes] = []
    sample_rate: Optional[int] = None
    sample_width: Optional[int] = None
    channels: Optional[int] = None

    for name, voice, text, extra_style in sections:
        section_path = scratch_dir / f"{name}.wav"
        already_done = section_path.exists()
        logger.info(
            "TTS piece %s (edition #%d) → %s",
            name, edition_id,
            "SKIPPING (already done)" if already_done else "SYNTHESIZING",
        )
        if already_done:
            stats.sections_skipped += 1
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                logger.info(
                    "Crosscut TTS: synthesizing %s (~%d chars, voice=%s%s)",
                    name, len(text), voice,
                    f", accent={extra_style}" if extra_style else "",
                )
                try:
                    result = tts.synthesize(
                        text, _Path(tmp_path), voice_id=voice,
                        extra_style=extra_style,
                    )
                except Exception as ex:
                    stats.errors += 1
                    logger.warning("Crosscut TTS: section %s failed: %s", name, ex)
                    continue
                # Move (not copy) into the scratch dir — atomic on the
                # same filesystem, so a crash mid-synthesize never
                # leaves a partial file at section_path.
                shutil.move(str(result.output_path), str(section_path))
                stats.sections_synthesized += 1
                stats.total_audio_seconds += result.duration_seconds
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Read the WAV — freshly synthesized or reused from a prior
        # attempt, same either way — and extract its PCM samples.
        with wave.open(str(section_path), "rb") as wf:
            if sample_rate is None:
                sample_rate  = wf.getframerate()
                sample_width = wf.getsampwidth()
                channels     = wf.getnchannels()
            section_pcms.append(wf.readframes(wf.getnframes()))

    if not section_pcms or sample_rate is None:
        logger.error("Crosscut TTS: no sections produced audio for edition #%d",
                     edition_id)
        return stats

    # Inter-section silence in 16-bit PCM.
    silence_samples = int(sample_rate * CROSSCUT_INTER_SECTION_PAUSE_MS / 1000)
    silence_bytes = b"\x00" * (silence_samples * (sample_width or 2) * (channels or 1))

    combined: list[bytes] = []
    for i, pcm in enumerate(section_pcms):
        combined.append(pcm)
        if i < len(section_pcms) - 1:
            combined.append(silence_bytes)

    out_path = audio_dir / f"crosscut_{edition_id:04d}.wav"
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(channels or 1)
        wf.setsampwidth(sample_width or 2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(combined))

    duration, sr_out = _wav_duration(out_path)
    stats.output_path = str(out_path)
    stats.total_audio_seconds = duration

    # Store audio_url on BOTH edition_pieces rows so the existing
    # publish/RSS infrastructure picks it up. We dedupe in Phase 5's
    # RSS path so the listener only sees one item per crosscut episode.
    try:
        rel_path = out_path.relative_to(config.audio_dir.parent.parent)
    except ValueError:
        rel_path = out_path
    audio_url = str(rel_path)
    with db.connect() as conn:
        conn.execute("""
            UPDATE edition_pieces
               SET audio_url = ?, duration_seconds = ?, narrator_voice = ?
             WHERE edition_id = ?
        """, (audio_url, int(round(duration)),
              f"{host_voice}/{voice_a}/{voice_b}", edition_id))
        # Mark both pieces approved so the audit/RSS paths consider them
        # final.
        conn.execute("""
            UPDATE edition_pieces SET review_status = 'approved'
             WHERE edition_id = ?
        """, (edition_id,))

    # Combined output is written and audio_url is durably persisted —
    # the per-section scratch files have served their purpose (letting
    # a crash-and-resume skip finished sections) and are no longer
    # needed.
    shutil.rmtree(scratch_dir, ignore_errors=True)

    mins, secs = divmod(int(duration), 60)
    logger.info(
        "Crosscut TTS: edition #%d → %s  (%dm %ds, %d synthesized, "
        "%d resumed-from-cache, %d errors)",
        edition_id, audio_url, mins, secs,
        stats.sections_synthesized, stats.sections_skipped, stats.errors,
    )
    return stats
