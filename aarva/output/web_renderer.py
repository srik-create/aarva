"""HTML edition-page renderer.

Generates a self-contained responsive HTML page per edition, styled to
match the v2 prototype's editorial-magazine aesthetic (Fraunces serif,
cream paper, ink black, vermilion accent). Each piece displays:
  - The editor's italic question (Stage 8a hook)
  - The why-now contextualisation (Stage 8b)
  - Title, byline, publication
  - Inline audio player (HTML5 <audio>)
  - "Read at source" link

Pieces are grouped by slot in this order: deep_feature, lens cards
(future / humans / behind), curiosity, smart_escape — matching the
editorial-rhythm principle from the kickoff doc.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


SLOT_ORDER = [
    "deep_feature",
    "lens_card_future",
    "lens_card_humans",
    "lens_card_behind",
    "curiosity",
    "smart_escape",
    "delight",
]

SLOT_DISPLAY = {
    "deep_feature": "Today's Feature",
    "lens_card_future": "Future Gazing",
    "lens_card_humans": "Humans & Humanity",
    "lens_card_behind": "Behind the News",
    "curiosity": "For Your Curiosity",
    "smart_escape": "A Smart Escape",
    "delight": "A Bit of Delight",
}


@dataclass
class WebRenderStats:
    edition_id: Optional[int] = None
    edition_date: Optional[date] = None
    pieces_rendered: int = 0
    html_path: Optional[Path] = None


def _load_edition_for_render(db: Database, edition_id: int) -> tuple[date, list[dict]]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, edition_date FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Edition {edition_id} not found.")
        edition_date = date.fromisoformat(str(row["edition_date"]))
        rows = conn.execute("""
            SELECT ep.slot, ep.position, ep.hook, ep.contextualisation,
                   ep.show_notes,
                   ep.audio_url, ep.duration_seconds, ep.narrator_voice,
                   a.id AS article_id, a.title, a.byline, a.canonical_url,
                   p.name AS publication_name,
                   s.lens, s.pillar, s.jtbd_primary
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE ep.edition_id = ?
               AND ep.flagged_at IS NULL    -- Q6 post-hoc flag-and-remove
             ORDER BY ep.position
        """, (edition_id,)).fetchall()
    return edition_date, [dict(r) for r in rows]


def _format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    mins, secs = divmod(int(seconds), 60)
    return f"{mins} min" if secs < 30 else f"{mins + 1} min"


def _e(s: Optional[str]) -> str:
    """HTML-escape a string, treating None as empty."""
    return html.escape(s or "", quote=True)


def _piece_html(piece: dict, public_url_base: str) -> str:
    audio_rel = piece.get("audio_url") or ""
    audio_url = f"{public_url_base.rstrip('/')}/{audio_rel.lstrip('/')}" if audio_rel else ""
    audio_mime = "audio/mpeg" if audio_rel.lower().endswith(".mp3") else "audio/wav"
    duration = _format_duration(piece.get("duration_seconds"))
    narrator = piece.get("narrator_voice") or ""

    audio_block = f"""
        <audio controls preload="metadata" class="audio">
          <source src="{_e(audio_url)}" type="{_e(audio_mime)}" />
          Your browser doesn't support the audio element.
        </audio>
        <div class="audio-meta">
          {("<span>" + _e(duration) + "</span>") if duration else ""}
          {("<span class='sep'>·</span><span>narrated by " + _e(narrator) + "</span>") if narrator else ""}
        </div>
    """ if audio_url else "<div class='audio-missing'>Audio not available</div>"

    show_notes_block = (
        f"<p class='show-notes'>{_e(piece.get('show_notes'))}</p>"
        if piece.get("show_notes") else ""
    )
    return f"""
      <article class="piece">
        <p class="hook">{_e(piece.get("hook") or "")}</p>
        <h2 class="title">{_e(piece.get("title") or "Untitled")}</h2>
        <p class="byline">
          <span class="publication">{_e(piece.get("publication_name") or "")}</span>
          {("<span class='sep'>·</span><span>" + _e(piece.get("byline") or "") + "</span>") if piece.get("byline") else ""}
        </p>
        <p class="contextualisation">{_e(piece.get("contextualisation") or "")}</p>
        {show_notes_block}
        {audio_block}
        {("<p class='source'><a href='" + _e(piece.get("canonical_url") or "") + "' target='_blank' rel='noopener'>Read at source →</a></p>") if piece.get("canonical_url") else ""}
      </article>
    """


def _slot_section_html(slot_name: str, pieces: list[dict], public_url_base: str) -> str:
    if not pieces:
        return ""
    pieces_html = "\n".join(_piece_html(p, public_url_base) for p in pieces)
    return f"""
      <section class="slot slot-{_e(slot_name)}">
        <h3 class="slot-title">{_e(SLOT_DISPLAY.get(slot_name, slot_name))}</h3>
        {pieces_html}
      </section>
    """


def render_edition_html(
    config: PipelineConfig,
    db: Database,
    edition_id: int,
) -> WebRenderStats:
    """Render an edition's pieces to a self-contained HTML page."""
    edition_date, pieces = _load_edition_for_render(db, edition_id)
    stats = WebRenderStats(edition_id=edition_id, edition_date=edition_date)

    public_url_base = (config.raw.get("output", {}) or {}).get(
        "public_url_base", "file:///"
    )

    # Group by slot, preserving SLOT_ORDER
    by_slot: dict[str, list[dict]] = {slot: [] for slot in SLOT_ORDER}
    for p in pieces:
        slot = p.get("slot") or ""
        if slot in by_slot:
            by_slot[slot].append(p)
        else:
            by_slot.setdefault(slot, []).append(p)

    body_sections = "\n".join(
        _slot_section_html(slot, by_slot.get(slot, []), public_url_base)
        for slot in SLOT_ORDER
    )

    page = _PAGE_TEMPLATE.format(
        edition_date_iso=edition_date.isoformat(),
        edition_date_display=edition_date.strftime("%A, %d %B %Y"),
        body_sections=body_sections,
        piece_count=len([p for ps in by_slot.values() for p in ps]),
    )

    out_dir = config.web_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"edition-{edition_date.isoformat()}.html"
    out_path.write_text(page, encoding="utf-8")
    stats.html_path = out_path
    stats.pieces_rendered = sum(len(ps) for ps in by_slot.values())

    # Also write a stable "latest.html" pointing at the most recent edition.
    latest = out_dir / "latest.html"
    latest.write_text(page, encoding="utf-8")

    logger.info(
        "Stage 10 web — edition #%d (%s): %d pieces rendered to %s",
        edition_id, edition_date, stats.pieces_rendered, out_path,
    )
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Crosscut renderer
# ─────────────────────────────────────────────────────────────────────────────

def _load_crosscut_for_render(db: Database, edition_id: int) -> Optional[dict]:
    """Pull everything needed to render one crosscut episode page —
    edition-level (topic, intro, outro, audio) + the two pieces (title,
    byline, publication, source url, bridge text)."""
    from aarva.services.queries import load_crosscut_episodes
    rows = load_crosscut_episodes(db, edition_id=edition_id)
    return rows[0] if rows else None


def _crosscut_body_html(cc: dict, public_url_base: str) -> str:
    """The body section for a crosscut episode page. Linear structure
    mirrors the audio: topic → audio → intro → bridge_a → article A
    → bridge_between → article B → outro."""
    audio_rel = cc.get("audio_url") or ""
    audio_url = (
        f"{public_url_base.rstrip('/')}/{audio_rel.lstrip('/')}"
        if audio_rel else ""
    )
    audio_mime = "audio/mpeg" if audio_rel.lower().endswith(".mp3") else "audio/wav"
    duration = _format_duration(cc.get("duration_seconds"))
    narrator = cc.get("narrator_voice") or ""

    audio_block = f"""
      <audio controls preload="metadata" class="audio">
        <source src="{_e(audio_url)}" type="{_e(audio_mime)}" />
        Your browser doesn't support the audio element.
      </audio>
      <div class="audio-meta">
        {("<span>" + _e(duration) + "</span>") if duration else ""}
        {("<span class='sep'>·</span><span>narrated by " + _e(narrator) + "</span>") if narrator else ""}
      </div>
    """ if audio_url else "<div class='audio-missing'>Audio not available</div>"

    def _article_card(title, byline, pub, url, bridge):
        bridge_block = (
            f"<p class='hook'>{_e(bridge)}</p>" if bridge else ""
        )
        source_link = (
            f"<p class='source'><a href='{_e(url)}' target='_blank' "
            f"rel='noopener'>Read at source →</a></p>" if url else ""
        )
        byline_block = (
            f"<span class='sep'>·</span><span>{_e(byline)}</span>"
            if byline else ""
        )
        return (
            "<article class='piece'>\n"
            + bridge_block + "\n"
            + f"<h2 class='title'>{_e(title or 'Untitled')}</h2>\n"
            + f"<p class='byline'><span class='publication'>{_e(pub or '')}</span>{byline_block}</p>\n"
            + source_link + "\n"
            + "</article>"
        )

    intro_block = (
        f"<p class='contextualisation'>{_e(cc.get('intro_text') or '')}</p>"
        if cc.get("intro_text") else ""
    )
    bridge_between_block = (
        f"<p class='hook'>{_e(cc.get('bridge_between') or '')}</p>"
        if cc.get("bridge_between") else ""
    )
    outro_block = (
        f"<p class='contextualisation'>{_e(cc.get('outro_text') or '')}</p>"
        if cc.get("outro_text") else ""
    )

    return f"""
      <section class="slot slot-crosscut">
        <h3 class="slot-title">Crosscut · {_e(cc.get('topic_label') or 'untitled')}</h3>
        {audio_block}
        {intro_block}
        {_article_card(cc.get('title_a'), cc.get('byline_a'),
                       cc.get('pub_a'), cc.get('url_a'), cc.get('bridge_a'))}
        {bridge_between_block}
        {_article_card(cc.get('title_b'), cc.get('byline_b'),
                       cc.get('pub_b'), cc.get('url_b'), None)}
        {outro_block}
      </section>
    """


def render_crosscut_html(
    config: PipelineConfig,
    db: Database,
    edition_id: int,
) -> WebRenderStats:
    """Render a crosscut episode to a self-contained HTML page.

    Output: `aarva/output/web/crosscut-YYYY-MM-DD.html`. Same masthead /
    footer as the daily edition page; body is one section showing the
    linear editorial structure: topic → audio → intro → article A →
    bridge between → article B → outro. The audio is the single
    stitched MP3; the page is a way for browser visitors and podcast-
    app deep links to see the editorial context."""
    cc = _load_crosscut_for_render(db, edition_id)
    if not cc:
        raise RuntimeError(
            f"Crosscut edition {edition_id} not found "
            f"(or not edition_type='crosscut')."
        )
    edition_date = date.fromisoformat(str(cc["edition_date"]))
    stats = WebRenderStats(edition_id=edition_id, edition_date=edition_date)

    public_url_base = (config.raw.get("output", {}) or {}).get(
        "public_url_base", "file:///"
    )
    body = _crosscut_body_html(cc, public_url_base)

    page = _PAGE_TEMPLATE.format(
        edition_date_iso=edition_date.isoformat(),
        edition_date_display=f"Crosscut · {edition_date.strftime('%A, %d %B %Y')}",
        body_sections=body,
        piece_count=2,
    )

    out_dir = config.web_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"crosscut-{edition_date.isoformat()}.html"
    out_path.write_text(page, encoding="utf-8")
    stats.html_path = out_path
    stats.pieces_rendered = 2

    logger.info(
        "Stage 10 web — crosscut #%d (%s) rendered to %s",
        edition_id, edition_date, out_path,
    )
    return stats


def crosscut_html_url_for(public_url_base: str, edition_date: date) -> str:
    """Stable URL for a given crosscut edition's HTML page. Used by
    rss_feed.py for the per-item <link> on crosscut episodes."""
    return (
        f"{public_url_base.rstrip('/')}/web/"
        f"crosscut-{edition_date.isoformat()}.html"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTML template (kept inline — single file deliverable, no Jinja dependency)
# ─────────────────────────────────────────────────────────────────────────────

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Aarva — {edition_date_display}</title>
<style>
  :root {{
    --paper:     #f5efe0;  /* warm cream — has presence without competing */
    --ink:       #0a0a0a;
    --ink-mid:   #2a2a2a;
    --ink-soft:  #4a4a4a;  /* one step lighter than mid; used for show-notes */
    --muted:     #6b6b6b;
    --rule:      #1a1a1a;
    --rule-soft: #d8d2c4;  /* tinted rule colour to match the cream */
    /* Bold accent palette */
    --accent:    #e63946;  /* saturated red — primary, used for hooks & links */
    --accent-2:  #1d3557;  /* deep navy — slot headers */
    --accent-3:  #f4a900;  /* warm gold — secondary highlights */
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
    background: var(--paper);
    color: var(--ink);
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
  }}

  .container {{
    max-width: 760px;
    margin: 0 auto;
    padding: 56px 24px 96px;
  }}

  header.masthead {{
    padding-bottom: 32px;
    border-bottom: 4px solid var(--ink);
    margin-bottom: 48px;
  }}
  .brand {{
    font-size: 56px;
    font-weight: 900;
    letter-spacing: -0.04em;
    line-height: 1;
    color: var(--ink);
  }}
  .brand::after {{
    content: '.';
    color: var(--accent);
  }}
  .brand-tagline {{
    font-size: 16px;
    color: var(--ink-mid);
    margin-top: 14px;
    font-weight: 500;
    font-style: italic;
    line-height: 1.4;
    max-width: 560px;
  }}
  .edition-date {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--muted);
    margin-top: 18px;
    font-weight: 600;
  }}
  .edition-meta {{
    font-size: 15px;
    color: var(--ink-mid);
    margin-top: 4px;
    font-weight: 400;
  }}

  .slot {{ margin: 56px 0; }}
  .slot-title {{
    display: inline-block;
    background: var(--accent-2);
    color: #ffffff;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-weight: 700;
    padding: 8px 16px;
    margin-bottom: 28px;
  }}

  .piece {{
    margin-bottom: 64px;
    padding-bottom: 40px;
    border-bottom: 1px solid var(--rule-soft);
  }}
  .piece:last-child {{
    border-bottom: none;
  }}

  .hook {{
    font-size: 22px;
    line-height: 1.35;
    color: var(--accent);
    margin-bottom: 24px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }}
  .title {{
    font-size: 34px;
    line-height: 1.15;
    letter-spacing: -0.025em;
    font-weight: 800;
    margin-bottom: 14px;
    color: var(--ink);
  }}
  .byline {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--muted);
    margin-bottom: 24px;
    font-weight: 600;
  }}
  .publication {{
    color: var(--accent-2);
    font-weight: 700;
  }}
  .sep {{
    color: var(--rule-soft);
    margin: 0 8px;
  }}

  .contextualisation {{
    font-size: 17px;
    line-height: 1.65;
    color: var(--ink-mid);
    margin-bottom: 16px;
  }}

  /* Show notes: neutral 2-3 sentence summary. Visually a step quieter
     than the contextualisation — smaller, slightly muted — so a reader's
     eye lands on the editorial framing first and the synopsis second. */
  .show-notes {{
    font-size: 15px;
    line-height: 1.6;
    color: var(--ink-soft);
    border-left: 2px solid var(--rule-soft);
    padding-left: 14px;
    margin: 12px 0 28px;
  }}

  .audio {{
    width: 100%;
    margin-bottom: 8px;
  }}
  audio::-webkit-media-controls-panel {{
    background-color: #f8f8f8;
  }}
  .audio-meta {{
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 16px;
    font-weight: 500;
  }}
  .audio-missing {{
    font-size: 13px;
    color: var(--muted);
    font-style: italic;
    margin-bottom: 16px;
  }}

  .source {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    margin-top: 16px;
    font-weight: 700;
  }}
  .source a {{
    color: var(--accent);
    text-decoration: none;
    border-bottom: 2px solid var(--accent);
    padding-bottom: 2px;
  }}
  .source a:hover {{
    background: var(--accent);
    color: var(--paper);
  }}

  footer {{
    margin-top: 80px;
    padding-top: 32px;
    border-top: 4px solid var(--ink);
    font-size: 13px;
    color: var(--muted);
    font-weight: 500;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  footer .tagline {{
    color: var(--ink-mid);
    font-weight: 600;
  }}

  @media (max-width: 600px) {{
    .brand {{ font-size: 44px; }}
    .title {{ font-size: 28px; }}
    .hook {{ font-size: 19px; }}
    .container {{ padding: 32px 18px 64px; }}
  }}
</style>
</head>
<body>
  <div class="container">
    <header class="masthead">
      <div class="brand">Aarva</div>
      <div class="brand-tagline">The world as your classroom, the finest journalism as your curriculum.</div>
      <div class="edition-date">{edition_date_iso}</div>
      <div class="edition-meta">{piece_count} pieces · {edition_date_display}</div>
    </header>

    {body_sections}

    <footer>
      <span class="tagline">Written by humans. Narrated by AI.</span>
      <span>v0.1</span>
    </footer>
  </div>
</body>
</html>"""
