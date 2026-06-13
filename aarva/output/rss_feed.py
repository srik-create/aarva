"""Podcast RSS feed generator.

Generates `aarva/output/feed.xml` — a Podcast 2.0 compliant RSS feed with
one item per edition piece across all editions. The feed URL is what
listeners subscribe to in their podcast app (Apple Podcasts, Pocket Casts,
Overcast, etc.).

Hand-crafted XML (no external feedgen dependency for v0.1). The format is
stable RSS 2.0 + iTunes namespace, which every major podcast app accepts.

Directory readiness: the feed includes all elements required by Apple
Podcasts Connect and Spotify for Podcasters validation — itunes:type,
itunes:owner with valid email, itunes:category, itunes:explicit,
itunes:image (3000×3000 PNG), itunes:summary, copyright, plus per-item
itunes:episodeType="full" and accurate enclosure length (file size in
bytes — Apple's validator warns on length=0).
"""
from __future__ import annotations

import html
import logging
import re
import uuid
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
    """Daily + bonus pieces with audio. Crosscut episodes are loaded
    separately by _load_published_crosscuts so they emit ONE RSS item
    per edition (two crosscut pieces share one audio file).

    The public feed.xml shows everything — global daily editions
    plus all bonus episodes regardless of who published them. Per-
    user filtering happens in the future per-user RSS endpoint
    (`/feed/{user_token}.xml`), not here.
    """
    from aarva.services.queries import (
        load_daily_pieces_with_audio,
        load_bonus_pieces_with_audio,
    )
    daily = load_daily_pieces_with_audio(db)
    # user_id=None → all bonus episodes regardless of attribution.
    # Includes CLI-published bonuses (user_id IS NULL) and any
    # future web-published bonuses (user_id set). For the public
    # RSS feed we surface them all.
    bonus = load_bonus_pieces_with_audio(db)
    return daily + bonus


def _load_published_crosscuts(db: Database) -> list[dict]:
    """All built crosscut episodes with audio. Returns one row per
    edition with both pieces' metadata joined in."""
    from aarva.services.queries import load_crosscut_episodes
    return load_crosscut_episodes(db)


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


def _mime_for(audio_url: str) -> str:
    """Map audio file extension to its podcast-compatible MIME type."""
    lower = audio_url.lower()
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".m4a"):
        return "audio/mp4"
    if lower.endswith(".wav"):
        return "audio/wav"
    return "audio/mpeg"


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str, max_chars: int = 4000) -> str:
    """Crude HTML → plain-text for itunes:summary. Apple recommends
    ≤4000 chars and prefers no markup in summary. Replace <br/> and
    paragraph breaks with newlines, then strip remaining tags.
    """
    if not s:
        return ""
    # Preserve paragraph breaks before stripping tags.
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    # Collapse runs of whitespace but keep paragraph breaks.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if len(s) > max_chars:
        s = s[:max_chars].rsplit(" ", 1)[0] + "…"
    return s


# Stable UUID5 namespace from the Podcast Index spec for podcast:guid.
# Combined with the feed URL (trimmed) it produces a deterministic UUID
# that identifies the show across feed-URL changes.
_PODCAST_GUID_NAMESPACE = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")


def _podcast_guid_for_feed(feed_url: str) -> str:
    """Compute a stable podcast:guid for the channel.

    Per podcastindex.org spec: UUID5 of the namespace UUID above with
    the feed URL stripped of any scheme prefix and trailing slash.
    """
    trimmed = re.sub(r"^https?://", "", feed_url).rstrip("/")
    return str(uuid.uuid5(_PODCAST_GUID_NAMESPACE, trimmed))


def _audio_byte_length(audio_url: str, package_root: Path) -> int:
    """Look up the file size in bytes from the local audio file.

    Apple Podcasts and many other validators warn on enclosure
    length="0" — the spec wants the byte length of the audio file. We
    treat audio_url as a path relative to the project root and stat
    it. Returns 0 if the file isn't there (still emits a valid feed).
    """
    try:
        rel = audio_url.lstrip("/")
        path = package_root / rel
        if path.exists():
            return path.stat().st_size
    except Exception:
        pass
    return 0


def _item_xml(piece: dict, public_url_base: str, package_root: Path,
              feed_image: str = "") -> str:
    audio_url = _audio_full_url(piece["audio_url"], public_url_base)
    byte_len = _audio_byte_length(piece["audio_url"], package_root)
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

    # Description structure (top-to-bottom in podcast app episode view):
    #   1. Hook        (italic teaser question — Aarva voice)
    #   2. Context     (60-100 word why-now framing — Aarva voice)
    #   3. Show notes  (2-3 sentence neutral summary of what the article is)
    #   4. Source link (back to the publication)
    description_parts = []
    if piece.get("hook"):
        description_parts.append(f"<em>{_xml_esc(piece['hook'])}</em>")
    if piece.get("contextualisation"):
        description_parts.append(_xml_esc(piece["contextualisation"]))
    if piece.get("show_notes"):
        description_parts.append(_xml_esc(piece["show_notes"]))
    if piece.get("canonical_url"):
        description_parts.append(
            f'Read at source: <a href="{_xml_esc(piece["canonical_url"])}">'
            f'{_xml_esc(piece["publication_name"] or piece["canonical_url"])}</a>'
        )
    description = "<br/><br/>".join(description_parts)

    item_title = piece.get("title") or "Untitled"
    # Per-item <link> — Apple/Cast validators flag items missing this.
    # We use the source article URL; it's the page a listener clicks
    # through to from their podcast app.
    item_link = piece.get("canonical_url") or ""
    summary_text = _strip_html(description, max_chars=4000)
    # Per-item iTunes image — required for proper share unfurls on
    # Apple Podcasts (the apple.com episode page's Open Graph image
    # is driven from this). We default to the channel cover; in the
    # future we can override per-episode if we generate custom art.
    image_tag = (
        f'<itunes:image href="{_xml_esc(feed_image)}"/>'
        if feed_image else ""
    )
    # Bonus episodes (user-picked ad-hoc articles via
    # aarva.publish_articles) tag as 'bonus' so Apple/Spotify show them
    # as side content, separate from the main daily series.
    episode_type = (
        "bonus" if piece.get("edition_type") == "bonus" else "full"
    )
    return f"""    <item>
      <title>{_xml_esc(item_title)}</title>
      <itunes:title>{_xml_esc(item_title)}</itunes:title>
      <link>{_xml_esc(item_link)}</link>
      <description><![CDATA[{description}]]></description>
      <content:encoded><![CDATA[{description}]]></content:encoded>
      <itunes:summary>{_xml_esc(summary_text)}</itunes:summary>
      <pubDate>{pub_date_rss}</pubDate>
      <guid isPermaLink="false">{_xml_esc(guid)}</guid>
      <enclosure url="{_xml_esc(audio_url)}" length="{byte_len}" type="{_xml_esc(_mime_for(audio_url))}"/>
      <itunes:duration>{_xml_esc(duration_str)}</itunes:duration>
      <itunes:author>{_xml_esc(piece.get("byline") or piece.get("publication_name") or "Aarva")}</itunes:author>
      <itunes:subtitle>{_xml_esc(piece.get("publication_name") or "")}</itunes:subtitle>
      {image_tag}
      <itunes:episodeType>{episode_type}</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>"""


def _crosscut_item_xml(cc: dict, public_url_base: str, package_root: Path,
                       feed_image: str = "") -> str:
    """Render ONE RSS item for a crosscut episode (not two — both
    pieces share the same audio file and represent a single listening
    unit)."""
    audio_url = _audio_full_url(cc["audio_url"], public_url_base)
    byte_len = _audio_byte_length(cc["audio_url"], package_root)
    pub_dt = cc.get("published_date") or cc.get("edition_date")
    if isinstance(pub_dt, str):
        try:
            pub_dt = datetime.fromisoformat(pub_dt.replace("Z", "+00:00"))
        except ValueError:
            pub_dt = datetime.now(timezone.utc)
    pub_date_rss = _format_rfc822(pub_dt) if pub_dt else _format_rfc822(
        datetime.now(timezone.utc)
    )
    duration_str = _format_duration_hhmmss(cc.get("duration_seconds"))
    guid = f"aarva-crosscut-{cc['edition_id']}"

    topic = cc.get("topic_label") or "untitled"
    title = f"Crosscut: {topic}"

    # Description: intro + the cross-piece bridge + outro + the two
    # source links. Editorially this gives the listener a flavour of
    # the connection before they hit play.
    desc_parts = []
    if cc.get("intro_text"):
        desc_parts.append(_xml_esc(cc["intro_text"]))
    if cc.get("bridge_between"):
        desc_parts.append(f"<em>{_xml_esc(cc['bridge_between'])}</em>")
    if cc.get("outro_text"):
        desc_parts.append(_xml_esc(cc["outro_text"]))
    sources = []
    if cc.get("url_a"):
        sources.append(
            f'<a href="{_xml_esc(cc["url_a"])}">{_xml_esc(cc["pub_a"])}: '
            f'{_xml_esc(cc["title_a"])}</a>'
        )
    if cc.get("url_b"):
        sources.append(
            f'<a href="{_xml_esc(cc["url_b"])}">{_xml_esc(cc["pub_b"])}: '
            f'{_xml_esc(cc["title_b"])}</a>'
        )
    if sources:
        desc_parts.append("Sources:<br/>" + "<br/>".join(sources))
    description = "<br/><br/>".join(desc_parts)

    # Per-item link — point at the crosscut HTML page (rendered by
    # web_renderer.render_crosscut_html in Stage 10). This is the
    # right "episode page" target: it shows the editorial structure
    # (topic, intro, both articles' bylines & sources, bridges, outro)
    # alongside the single combined audio player.
    from aarva.output.web_renderer import crosscut_html_url_for
    from datetime import date as _date_type
    ed_date = cc.get("edition_date")
    if isinstance(ed_date, str):
        try:
            ed_date = _date_type.fromisoformat(ed_date)
        except ValueError:
            ed_date = None
    item_link = (
        crosscut_html_url_for(public_url_base, ed_date)
        if ed_date else (cc.get("url_a") or "")
    )
    summary_text = _strip_html(description, max_chars=4000)
    image_tag = (
        f'<itunes:image href="{_xml_esc(feed_image)}"/>'
        if feed_image else ""
    )
    return f"""    <item>
      <title>{_xml_esc(title)}</title>
      <itunes:title>{_xml_esc(title)}</itunes:title>
      <link>{_xml_esc(item_link)}</link>
      <description><![CDATA[{description}]]></description>
      <content:encoded><![CDATA[{description}]]></content:encoded>
      <itunes:summary>{_xml_esc(summary_text)}</itunes:summary>
      <pubDate>{pub_date_rss}</pubDate>
      <guid isPermaLink="false">{_xml_esc(guid)}</guid>
      <enclosure url="{_xml_esc(audio_url)}" length="{byte_len}" type="{_xml_esc(_mime_for(audio_url))}"/>
      <itunes:duration>{_xml_esc(duration_str)}</itunes:duration>
      <itunes:author>Aarva</itunes:author>
      <itunes:subtitle>Crosscut · {_xml_esc(topic)}</itunes:subtitle>
      {image_tag}
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>"""


def _item_pub_dt(piece: dict) -> datetime:
    """Sort key for combined daily + crosscut feed."""
    raw = piece.get("published_date") or piece.get("edition_date")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        return raw
    return datetime.now(timezone.utc)


def generate_feed(
    config: PipelineConfig,
    db: Database,
) -> FeedStats:
    output_cfg = config.raw.get("output", {}) or {}
    public_url_base = output_cfg.get("public_url_base", "file:///")
    feed_title = output_cfg.get("feed_title", "Aarva")
    feed_description = output_cfg.get(
        "feed_description",
        "The world as your classroom, the finest journalism as your "
        "curriculum. Written by humans. Narrated by AI.",
    )
    feed_author = output_cfg.get("feed_author", "Aarva")
    feed_email = output_cfg.get("feed_email", "aarva@example.com")
    feed_link = output_cfg.get("feed_link", public_url_base)
    feed_image = output_cfg.get("feed_image", "")
    # Optional knobs with sensible defaults — overridable in pipeline.yaml.
    feed_summary = output_cfg.get("feed_summary", feed_description)
    feed_copyright = output_cfg.get(
        "feed_copyright",
        f"© {datetime.now().year} {feed_author}",
    )
    feed_type = output_cfg.get("feed_type", "episodic")
    # Secondary iTunes category — Apple allows two for better discovery.
    feed_category = output_cfg.get("feed_category", "News")
    feed_subcategory = output_cfg.get("feed_subcategory", "News Commentary")
    feed_category_2 = output_cfg.get("feed_category_2", "Education")
    feed_subcategory_2 = output_cfg.get("feed_subcategory_2", "Self-Improvement")

    # Resolve the package root from the feed-output path so we can stat
    # the local audio files to compute correct enclosure byte lengths.
    # audio_url is stored relative to the aarva/ package dir
    # (e.g., "output/audio/2026-06-09/article_0405.mp3"), and feed.xml
    # lives at aarva/output/feed.xml — so the package root is two levels up.
    package_root = config.rss_feed_path.resolve().parent.parent

    # Daily-edition pieces (one item each) + crosscut episodes (one item
    # per edition). We tag each with a kind so the renderer dispatches
    # correctly, then sort by publish/edition date.
    daily_pieces = _load_all_published_pieces(db)
    crosscut_eds = _load_published_crosscuts(db)
    combined = (
        [{"_kind": "daily",     **p} for p in daily_pieces]
        + [{"_kind": "crosscut", **c} for c in crosscut_eds]
    )
    combined.sort(key=_item_pub_dt, reverse=True)

    items_xml = "\n".join(
        _crosscut_item_xml(item, public_url_base, package_root, feed_image)
        if item["_kind"] == "crosscut"
        else _item_xml(item, public_url_base, package_root, feed_image)
        for item in combined
    )
    pieces = combined   # for the FeedStats count below
    last_build_date = _format_rfc822(datetime.now(timezone.utc))

    image_block = (
        f"<itunes:image href=\"{_xml_esc(feed_image)}\"/>\n    "
        f"<image><url>{_xml_esc(feed_image)}</url><title>{_xml_esc(feed_title)}</title>"
        f"<link>{_xml_esc(feed_link)}</link></image>"
    ) if feed_image else ""

    category_block = (
        f"<itunes:category text=\"{_xml_esc(feed_category)}\">"
        f"<itunes:category text=\"{_xml_esc(feed_subcategory)}\"/>"
        f"</itunes:category>"
    )
    if feed_category_2:
        if feed_subcategory_2:
            category_block += (
                f"\n    <itunes:category text=\"{_xml_esc(feed_category_2)}\">"
                f"<itunes:category text=\"{_xml_esc(feed_subcategory_2)}\"/>"
                f"</itunes:category>"
            )
        else:
            category_block += (
                f"\n    <itunes:category text=\"{_xml_esc(feed_category_2)}\"/>"
            )

    feed_self_url = public_url_base.rstrip('/') + "/feed.xml"
    podcast_guid = output_cfg.get("podcast_guid") or _podcast_guid_for_feed(
        feed_self_url
    )
    # podcast:locked — 'no' lets other hosts ingest the feed if they
    # prove ownership of the iTunes:owner email (allows hosts/aggregators
    # to mirror). 'yes' locks to current host. Default 'no' for now.
    podcast_locked = output_cfg.get("podcast_locked", "no")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{_xml_esc(feed_title)}</title>
    <link>{_xml_esc(feed_link)}</link>
    <description>{_xml_esc(feed_description)}</description>
    <itunes:summary>{_xml_esc(feed_summary)}</itunes:summary>
    <language>en-us</language>
    <copyright>{_xml_esc(feed_copyright)}</copyright>
    <lastBuildDate>{last_build_date}</lastBuildDate>
    <generator>Aarva pipeline</generator>
    <itunes:type>{_xml_esc(feed_type)}</itunes:type>
    <itunes:author>{_xml_esc(feed_author)}</itunes:author>
    <itunes:owner>
      <itunes:name>{_xml_esc(feed_author)}</itunes:name>
      <itunes:email>{_xml_esc(feed_email)}</itunes:email>
    </itunes:owner>
    {category_block}
    <itunes:explicit>false</itunes:explicit>
    {image_block}
    <podcast:guid>{_xml_esc(podcast_guid)}</podcast:guid>
    <podcast:locked owner="{_xml_esc(feed_email)}">{_xml_esc(podcast_locked)}</podcast:locked>
    <atom:link href="{_xml_esc(feed_self_url)}" rel="self" type="application/rss+xml"/>
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
