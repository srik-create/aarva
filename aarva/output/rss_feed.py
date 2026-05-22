"""Podcast RSS feed generator.

Generates `aarva/output/feed.xml` — a Podcast 2.0 compliant RSS feed with
one item per edition piece across all editions. The feed URL is what
listeners subscribe to in their podcast app (Apple Podcasts, Pocket Casts,
Overcast, etc.).

Hand-crafted XML (no external feedgen dependency for v0.1). The format is
stable RSS 2.0 + iTunes namespace, which every major podcast app accepts.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class FeedStats:
    items_written: int = 0
    feed_path: Optional[Path] = None


def _load_all_published_pieces(db: Database) -> list[dict]:
    """Pull every piece that has audio attached, ordered newest first."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
                   ep.hook, ep.contextualisation, ep.audio_url,
                   ep.duration_seconds, ep.narrator_voice,
                   a.title, a.byline, a.canonical_url,
                   p.name AS publication_name,
                   e.edition_date, e.published_date
              FROM edition_pieces ep
              JOIN editions e ON e.id = ep.edition_id
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
             ORDER BY e.edition_date DESC, ep.position
        """).fetchall()
    return [dict(r) for r in rows]


def _xml_esc(s: Optional[str]) -> str:
    return html.escape(s or "", quote=True)


def _format_rfc822(dt: datetime) -> str:
    """RSS pubDate format: RFC 822."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def _format_duration_hhmmss(seconds: Optional[int]) -> str:
    if not seconds:
        return "00:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _audio_full_url(audio_url: str, public_url_base: str) -> str:
    base = public_url_base.rstrip("/")
    rel = audio_url.lstrip("/")
    return f"{base}/{rel}"


def _item_xml(piece: dict, public_url_base: str) -> str:
    audio_url = _audio_full_url(piece["audio_url"], public_url_base)
    pub_dt = piece.get("published_date") or piece.get("edition_date")
    if isinstance(pub_dt, str):
        try:
            pub_dt = datetime.fromisoformat(pub_dt.replace("Z", "+00:00"))
        except ValueError:
            pub_dt = datetime.now(timezone.utc)
    pub_date_rss = _format_rfc822(pub_dt) if pub_dt else _format_rfc822(
        datetime.now(timezone.utc)
    )
    duration_str = _format_duration_hhmmss(piece.get("duration_seconds"))

    # Stable GUID derived from edition + article id.
    guid = f"aarva-{piece['edition_id']}-{piece['article_id']}"

    description_parts = []
    if piece.get("hook"):
        description_parts.append(f"<em>{_xml_esc(piece['hook'])}</em>")
    if piece.get("contextualisation"):
        description_parts.append(_xml_esc(piece["contextualisation"]))
    if piece.get("canonical_url"):
        description_parts.append(
            f'Read at source: <a href="{_xml_esc(piece["canonical_url"])}">'
            f'{_xml_esc(piece["publication_name"] or piece["canonical_url"])}</a>'
        )
    description = "<br/><br/>".join(description_parts)

    return f"""    <item>
      <title>{_xml_esc(piece.get("title") or "Untitled")}</title>
      <description><![CDATA[{description}]]></description>
      <pubDate>{pub_date_rss}</pubDate>
      <guid isPermaLink="false">{_xml_esc(guid)}</guid>
      <enclosure url="{_xml_esc(audio_url)}" length="0" type="audio/wav"/>
      <itunes:duration>{_xml_esc(duration_str)}</itunes:duration>
      <itunes:author>{_xml_esc(piece.get("byline") or piece.get("publication_name") or "Aarva")}</itunes:author>
      <itunes:subtitle>{_xml_esc(piece.get("publication_name") or "")}</itunes:subtitle>
      <itunes:explicit>false</itunes:explicit>
    </item>"""


def generate_feed(
    config: PipelineConfig,
    db: Database,
) -> FeedStats:
    output_cfg = config.raw.get("output", {}) or {}
    public_url_base = output_cfg.get("public_url_base", "file:///")
    feed_title = output_cfg.get("feed_title", "Aarva")
    feed_description = output_cfg.get(
        "feed_description",
        "AI-narrated journalism. The world as your classroom.",
    )
    feed_author = output_cfg.get("feed_author", "Aarva")
    feed_email = output_cfg.get("feed_email", "aarva@example.com")
    feed_link = output_cfg.get("feed_link", public_url_base)
    feed_image = output_cfg.get("feed_image", "")

    pieces = _load_all_published_pieces(db)
    items_xml = "\n".join(_item_xml(p, public_url_base) for p in pieces)
    last_build_date = _format_rfc822(datetime.now(timezone.utc))

    image_block = (
        f"<itunes:image href=\"{_xml_esc(feed_image)}\"/>\n    "
        f"<image><url>{_xml_esc(feed_image)}</url><title>{_xml_esc(feed_title)}</title>"
        f"<link>{_xml_esc(feed_link)}</link></image>"
    ) if feed_image else ""

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{_xml_esc(feed_title)}</title>
    <link>{_xml_esc(feed_link)}</link>
    <description>{_xml_esc(feed_description)}</description>
    <language>en-us</language>
    <lastBuildDate>{last_build_date}</lastBuildDate>
    <itunes:author>{_xml_esc(feed_author)}</itunes:author>
    <itunes:owner>
      <itunes:name>{_xml_esc(feed_author)}</itunes:name>
      <itunes:email>{_xml_esc(feed_email)}</itunes:email>
    </itunes:owner>
    <itunes:category text="News"><itunes:category text="News Commentary"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    {image_block}
    <atom:link href="{_xml_esc(public_url_base.rstrip('/'))}/feed.xml" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>
"""

    out_path = config.rss_feed_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(feed_xml, encoding="utf-8")
    logger.info(
        "Stage 10 RSS — %d items written to %s",
        len(pieces), out_path,
    )

    return FeedStats(items_written=len(pieces), feed_path=out_path)
