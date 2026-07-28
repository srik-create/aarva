"""Add ad-hoc extra items to the podcast RSS feed.

See docs/session_plan_rss_extra_items.md. Writes to the local main_db
`rss_extra_items` table only — never touches listener_db, editions,
edition_pieces, articles, or publications. Stage 10
(`python -m aarva.daily --stage 10`) picks up any rows here the next
time it renders feed.xml.

Two invocation modes:

  1. Graduate a listener-created (or main-DB) crosscut by edition_id —
     fetches composed metadata from a Render admin endpoint:

       python -m aarva.rss_add --from-edition 1000011

  2. Fully manual — any audio URL, any title:

       python -m aarva.rss_add \\
         --audio-url "https://audio.aarva.app/path/to/file.mp3" \\
         --title "Some episode title" \\
         --description "Some description text" \\
         --duration 1620 \\
         --byte-length 12345678 \\
         --kind episode

Management:

  python -m aarva.rss_add --list
  python -m aarva.rss_add --remove aarva-crosscut-1000011

Both modes are idempotent by GUID — re-running with the same GUID
updates the row (INSERT OR REPLACE).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

# Allow running as a script.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from aarva.config import load_pipeline_config
from aarva.db import Database

DEFAULT_BASE_URL = "https://aarva.app"
CROSSCUT_PREFIX = "Crosscut: "


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "episode"


def _fetch_episode_metadata(edition_id: int, base_url: str, token: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/admin/episode-metadata"
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            url,
            params={"edition_id": edition_id},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 401:
        raise SystemExit(
            "Auth failed (401) — check AARVA_RENDER_SYNC_TOKEN matches "
            "the token configured on Render."
        )
    if resp.status_code == 404:
        raise SystemExit(
            f"edition {edition_id} not found, or isn't a listener-created "
            "crosscut (server said: "
            f"{resp.json().get('detail', resp.text)!r})"
        )
    if resp.status_code == 400:
        raise SystemExit(
            f"Bad request (server said: {resp.json().get('detail', resp.text)!r})"
        )
    if resp.status_code != 200:
        raise SystemExit(
            f"Unexpected response {resp.status_code} from {url}: {resp.text[:300]}"
        )
    return resp.json()


def _head_byte_length(url: str) -> Optional[int]:
    """Best-effort Content-Length via HTTP HEAD. Returns None on any
    failure so the caller can warn and continue with byte_length=0."""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.head(url, follow_redirects=True)
        if resp.status_code == 200:
            length = resp.headers.get("content-length")
            if length:
                return int(length)
    except Exception:
        pass
    return None


def _upsert_row(db: Database, row: dict[str, Any]) -> None:
    with db.connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO rss_extra_items
                (guid, episode_date, title, description_html, audio_url,
                 byte_length, duration_seconds, author, subtitle,
                 itunes_episode_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["guid"], row["episode_date"], row["title"],
            row.get("description_html"), row["audio_url"],
            row.get("byte_length") or 0, row.get("duration_seconds"),
            row.get("author") or "Aarva", row.get("subtitle"),
            row.get("itunes_episode_type") or "full",
        ))
        conn.commit()


def cmd_from_edition(args: argparse.Namespace, db: Database) -> int:
    token = os.environ.get("AARVA_RENDER_SYNC_TOKEN", "")
    if not token:
        print(
            "ERROR: AARVA_RENDER_SYNC_TOKEN is not set. Add it to "
            "~/.aarva.env (same token configured on Render).",
            file=sys.stderr,
        )
        return 1

    meta = _fetch_episode_metadata(args.from_edition, args.base_url, token)
    _upsert_row(db, meta)
    print(
        f"Added edition {args.from_edition} as RSS extra item "
        f"(guid={meta['guid']}, date={meta['episode_date']})."
    )
    if meta.get("byte_length") in (0, None):
        print(
            "  Warning: byte_length is 0 — the server couldn't stat or "
            "HEAD the audio file. The feed will still validate, but "
            "some podcast-app validators warn on enclosure length=0."
        )
    print("Run `python -m aarva.daily --stage 10` to publish the updated feed.")
    return 0


def cmd_manual(args: argparse.Namespace, db: Database) -> int:
    title = args.title
    if args.kind == "crosscut" and not title.startswith(CROSSCUT_PREFIX):
        title = CROSSCUT_PREFIX + title

    episode_date = args.episode_date or date.today().isoformat()

    byte_length = args.byte_length
    if byte_length is None and re.match(r"^https?://", args.audio_url):
        byte_length = _head_byte_length(args.audio_url)
        if byte_length is None:
            print(
                "  Warning: --byte-length omitted and HTTP HEAD couldn't "
                "determine it. Row will be written with byte_length=0 — "
                "feed still valid, but some validators warn on length=0.",
                file=sys.stderr,
            )

    guid = args.guid or f"aarva-extra-{_slugify(title)}-{episode_date}"

    row = {
        "guid": guid,
        "episode_date": episode_date,
        "title": title,
        "description_html": args.description,
        "audio_url": args.audio_url,
        "byte_length": byte_length or 0,
        "duration_seconds": args.duration,
        "author": args.author or "Aarva",
        "subtitle": args.subtitle,
        "itunes_episode_type": "full",
    }
    _upsert_row(db, row)
    print(f"Added RSS extra item (guid={guid}, date={episode_date}).")
    print("Run `python -m aarva.daily --stage 10` to publish the updated feed.")
    return 0


def cmd_list(db: Database) -> int:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT guid, episode_date, title, duration_seconds, byte_length, added_at
              FROM rss_extra_items
             ORDER BY episode_date DESC, added_at DESC
        """).fetchall()
    if not rows:
        print("No RSS extra items.")
        return 0
    for r in rows:
        mins = f"{r['duration_seconds'] // 60}m" if r["duration_seconds"] else "?"
        print(f"{r['episode_date']}  {r['guid']:<40}  {mins:>5}  {r['title']}")
    return 0


def cmd_remove(args: argparse.Namespace, db: Database) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM rss_extra_items WHERE guid = ?", (args.remove,),
        )
        conn.commit()
    if cur.rowcount:
        print(f"Removed {args.remove}. Run `python -m aarva.daily --stage 10` to update the feed.")
    else:
        print(f"No row with guid={args.remove!r} — nothing to remove.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--from-edition", type=int, default=None,
                    help="Graduate a listener-created (or main-DB) crosscut "
                         "by edition_id — fetches composed metadata from "
                         "the Render admin endpoint.")
    ap.add_argument("--audio-url", type=str, default=None,
                    help="Manual mode: full audio URL.")
    ap.add_argument("--title", type=str, default=None,
                    help="Manual mode: episode title.")
    ap.add_argument("--description", type=str, default=None,
                    help="Manual mode: description HTML/text (optional).")
    ap.add_argument("--duration", type=int, default=None,
                    help="Manual mode: duration in seconds (optional but recommended).")
    ap.add_argument("--byte-length", type=int, default=None,
                    help="Manual mode: audio file size in bytes. Auto-HEAD "
                         "attempted if omitted and --audio-url is https://.")
    ap.add_argument("--kind", choices=["crosscut", "episode"], default="episode",
                    help="Manual mode: 'crosscut' auto-prefixes the title "
                         "with 'Crosscut: ' (default: episode, no prefix).")
    ap.add_argument("--episode-date", type=str, default=None,
                    help="Manual mode: YYYY-MM-DD (default: today).")
    ap.add_argument("--guid", type=str, default=None,
                    help="Manual mode: custom GUID (default: generated from "
                         "title + episode-date).")
    ap.add_argument("--author", type=str, default=None,
                    help="Manual mode: itunes:author (default: Aarva).")
    ap.add_argument("--subtitle", type=str, default=None,
                    help="Manual mode: itunes:subtitle (optional).")
    ap.add_argument("--list", action="store_true",
                    help="List all current RSS extra items.")
    ap.add_argument("--remove", type=str, default=None, metavar="GUID",
                    help="Delete the row with this GUID.")
    ap.add_argument("--base-url", type=str, default=DEFAULT_BASE_URL,
                    help=f"Render app base URL for --from-edition (default {DEFAULT_BASE_URL}).")
    args = ap.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)

    if args.list:
        return cmd_list(db)
    if args.remove:
        return cmd_remove(args, db)
    if args.from_edition is not None:
        return cmd_from_edition(args, db)
    if args.audio_url or args.title:
        if not args.audio_url or not args.title:
            print("Manual mode requires both --audio-url and --title.", file=sys.stderr)
            return 1
        return cmd_manual(args, db)

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
