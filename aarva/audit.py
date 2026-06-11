"""Post-hoc audit + flag-and-remove CLI (Q6).

The complement to `aarva.review`:

  - `aarva.review` is the *pre*-publication gate. The user approves the
    proposed edition before Stage 8/9/10 run.
  - `aarva.audit` is the *post*-publication safety net. The user reviews
    past editions and flags pieces they want pulled from the live feed.

A flagged piece is soft-deleted via `edition_pieces.flagged_at`. The RSS
feed generator and web renderer both filter `flagged_at IS NULL` so a
flagged piece simply disappears from listener-facing surfaces. The audio
file itself stays on gh-pages (orphaned but harmless), so unflagging is
fully reversible.

After flagging, the user re-publishes (this CLI can do it automatically,
or you can run `bash scripts/publish.sh` yourself).

Usage (venv active):
    python -m aarva.audit                       # interactive: list recent editions
    python -m aarva.audit --edition-id 12       # jump straight into one edition
    python -m aarva.audit --list-flagged        # show all currently-flagged pieces
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Make the package importable when run via `python -m aarva.audit` or as a script.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE  # noqa: F401


# Max recent editions to show in the picker. Older editions are still
# reachable via --edition-id N.
RECENT_EDITIONS_TO_LIST = 14


@dataclass
class PieceRow:
    edition_id: int
    edition_date: str
    article_id: int
    slot: str
    position: int
    title: str
    publication_name: str
    canonical_url: str
    flagged_at: Optional[str]
    flag_reason: Optional[str]


# ─── DB access ────────────────────────────────────────────────────────────────

def _recent_editions(db: Database, limit: int) -> list[dict]:
    """Recent editions with piece counts and flag counts."""
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT e.id, e.edition_date,
                   COUNT(ep.article_id) AS total_pieces,
                   SUM(CASE WHEN ep.flagged_at IS NOT NULL THEN 1 ELSE 0 END) AS flagged_count
              FROM editions e
              LEFT JOIN edition_pieces ep ON ep.edition_id = e.id
             GROUP BY e.id
             ORDER BY e.edition_date DESC, e.id DESC
             LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def _load_edition_pieces(db: Database, edition_id: int) -> list[PieceRow]:
    with db.connect() as conn:
        edition = conn.execute(
            "SELECT id, edition_date FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
        if not edition:
            return []
        edition_date = str(edition["edition_date"])

        rows = conn.execute("""
            SELECT ep.article_id, ep.slot, ep.position,
                   ep.flagged_at, ep.flag_reason,
                   a.title, a.canonical_url,
                   p.name AS publication_name
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
             WHERE ep.edition_id = ?
             ORDER BY ep.position
        """, (edition_id,)).fetchall()

    return [
        PieceRow(
            edition_id=edition_id,
            edition_date=edition_date,
            article_id=int(r["article_id"]),
            slot=r["slot"],
            position=int(r["position"] or 0),
            title=r["title"] or "",
            publication_name=r["publication_name"] or "",
            canonical_url=r["canonical_url"] or "",
            flagged_at=r["flagged_at"],
            flag_reason=r["flag_reason"],
        )
        for r in rows
    ]


def _all_flagged(db: Database) -> list[PieceRow]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.edition_id, e.edition_date, ep.article_id, ep.slot,
                   ep.position, ep.flagged_at, ep.flag_reason,
                   a.title, a.canonical_url,
                   p.name AS publication_name
              FROM edition_pieces ep
              JOIN editions e ON e.id = ep.edition_id
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
             WHERE ep.flagged_at IS NOT NULL
             ORDER BY ep.flagged_at DESC
        """).fetchall()
    return [
        PieceRow(
            edition_id=int(r["edition_id"]),
            edition_date=str(r["edition_date"]),
            article_id=int(r["article_id"]),
            slot=r["slot"],
            position=int(r["position"] or 0),
            title=r["title"] or "",
            publication_name=r["publication_name"] or "",
            canonical_url=r["canonical_url"] or "",
            flagged_at=r["flagged_at"],
            flag_reason=r["flag_reason"],
        )
        for r in rows
    ]


def _set_flag(db: Database, edition_id: int, article_id: int,
              reason: Optional[str]) -> None:
    """Flag a piece. reason may be None or empty for 'no reason given'."""
    with db.connect() as conn:
        conn.execute("""
            UPDATE edition_pieces
               SET flagged_at = CURRENT_TIMESTAMP,
                   flag_reason = ?
             WHERE edition_id = ? AND article_id = ?
        """, (reason or None, edition_id, article_id))


def _clear_flag(db: Database, edition_id: int, article_id: int) -> None:
    with db.connect() as conn:
        conn.execute("""
            UPDATE edition_pieces
               SET flagged_at = NULL,
                   flag_reason = NULL
             WHERE edition_id = ? AND article_id = ?
        """, (edition_id, article_id))


# ─── Rendering ────────────────────────────────────────────────────────────────

def _print_edition_list(editions: list[dict]) -> None:
    print()
    print(BOLD("═" * 70))
    print(BOLD("  Aarva — Recent editions"))
    print(BOLD("═" * 70))
    print()
    for i, e in enumerate(editions, 1):
        flagged = int(e.get("flagged_count") or 0)
        total = int(e.get("total_pieces") or 0)
        flag_note = ""
        if flagged > 0:
            flag_note = "  " + RED(f"⚑ {flagged} flagged")
        print(f"  {BOLD(f'[{i}]')}  Edition #{e['id']:<3}  {e['edition_date']}"
              f"   ({total} pieces){flag_note}")
    print()
    print(DIM(f"Enter a number to inspect, or 'q' to quit. "
              f"(--edition-id N jumps directly to an edition.)"))


def _print_edition_detail(pieces: list[PieceRow], edition_id: int) -> None:
    if not pieces:
        print(RED(f"Edition #{edition_id} has no pieces."))
        return
    print()
    print(BOLD("═" * 70))
    print(BOLD(f"  Edition #{edition_id}   ·   {pieces[0].edition_date}"))
    print(BOLD("═" * 70))
    print()
    for i, p in enumerate(pieces, 1):
        if p.flagged_at:
            status = RED("⚑ FLAGGED")
            reason_line = f"        {DIM('reason:')} {p.flag_reason or '(none)'}"
        else:
            status = GREEN("✓ live")
            reason_line = None
        print(f"  {BOLD(f'[{i}]')}  {status}   "
              f"{BOLD(p.slot.replace('_', ' '))}   "
              f"{DIM('—')}  {YELLOW(p.publication_name)}")
        print(f"        {BOLD(p.title[:64])}")
        print(f"        {BLUE(p.canonical_url)}")
        if reason_line:
            print(reason_line)
        print()


def _print_flagged_list(flagged: list[PieceRow]) -> None:
    if not flagged:
        print(GREEN("No flagged pieces. The live feed is clean."))
        return
    print()
    print(BOLD(f"Currently flagged pieces ({len(flagged)}):"))
    print()
    for p in flagged:
        when = ""
        if p.flagged_at:
            try:
                dt = datetime.fromisoformat(str(p.flagged_at).replace("Z", "+00:00"))
                when = f"  {DIM(dt.strftime('%Y-%m-%d %H:%M'))}"
            except (ValueError, AttributeError):
                pass
        print(f"  Edition #{p.edition_id}  ({p.edition_date})  "
              f"[{p.publication_name}]{when}")
        print(f"    {BOLD(p.title[:64])}")
        print(f"    {DIM('reason:')} {p.flag_reason or '(none)'}")
        print()


# ─── Republish ────────────────────────────────────────────────────────────────

def _maybe_republish(project_root: Path) -> bool:
    """Offer to run scripts/publish.sh. Returns True if a publish happened."""
    print()
    try:
        choice = input(
            BOLD("Republish to gh-pages so the flag(s) take effect? [Y/n]: ")
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if choice in ("n", "no"):
        print(DIM("  Skipped. Run `bash scripts/publish.sh` when you're ready."))
        return False

    publish_sh = project_root / "scripts" / "publish.sh"
    if not publish_sh.exists():
        print(RED(f"  publish.sh not found at {publish_sh}"))
        return False

    print(DIM(f"  → bash {publish_sh}"))
    try:
        # First we need to regenerate the RSS feed + HTML so the flagged
        # piece disappears from the published artifacts. We do that via a
        # quick Stage 10 invocation, then publish.sh pushes the gh-pages
        # branch.
        subprocess.run(
            [sys.executable, "-m", "aarva.daily", "--stage", "10"],
            cwd=project_root, check=True,
        )
        subprocess.run(["bash", str(publish_sh)], cwd=project_root, check=True)
        print(GREEN("  ✓ Published. The flagged piece is now removed from "
                    "the live feed."))
        return True
    except subprocess.CalledProcessError as e:
        print(RED(f"  Republish failed: {e}"))
        print(DIM("  Run `bash scripts/publish.sh` manually to retry."))
        return False


# ─── Edition inspect loop ─────────────────────────────────────────────────────

def _inspect_edition(db: Database, edition_id: int,
                     project_root: Path) -> bool:
    """Returns True if anything was flagged/unflagged (caller may then offer
    republish)."""
    changed = False
    while True:
        pieces = _load_edition_pieces(db, edition_id)
        if not pieces:
            return changed

        _print_edition_detail(pieces, edition_id)
        print(DIM(
            "Type a number to flag/unflag that piece, or 'q' to go back."
        ))

        try:
            raw = input(BOLD("> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return changed

        if raw in ("q", "quit", "b", "back", ""):
            return changed

        try:
            idx = int(raw)
        except ValueError:
            print(RED(f"  Not a number: '{raw}'"))
            continue

        if idx < 1 or idx > len(pieces):
            print(RED(f"  Out of range (1–{len(pieces)})"))
            continue

        piece = pieces[idx - 1]
        if piece.flagged_at:
            # Unflag
            try:
                confirm = input(
                    f"  Unflag this piece? Reason was: "
                    f"{DIM(piece.flag_reason or '(none)')}. [Y/n]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if confirm not in ("n", "no"):
                _clear_flag(db, piece.edition_id, piece.article_id)
                print(GREEN(f"  ✓ Unflagged."))
                changed = True
        else:
            # Flag
            print(DIM("  Optional flag reason (one line, Enter to skip):"))
            try:
                reason = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            try:
                confirm = input(BOLD("  Flag this piece? [Y/n]: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if confirm in ("n", "no"):
                print(DIM("  Cancelled."))
                continue
            _set_flag(db, piece.edition_id, piece.article_id, reason or None)
            print(RED(f"  ⚑ Flagged."))
            changed = True


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--edition-id", type=int, default=None,
                        help="Inspect a specific edition by id (default: "
                             "interactive picker over recent editions)")
    parser.add_argument("--list-flagged", action="store_true",
                        help="List all currently-flagged pieces across "
                             "all editions and exit")
    args = parser.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)
    project_root = Path(__file__).resolve().parent.parent

    if args.list_flagged:
        _print_flagged_list(_all_flagged(db))
        return 0

    changed_anywhere = False

    if args.edition_id is not None:
        changed_anywhere = _inspect_edition(db, args.edition_id, project_root)
    else:
        # Interactive: pick from recent editions, optionally jump into one.
        while True:
            editions = _recent_editions(db, RECENT_EDITIONS_TO_LIST)
            if not editions:
                print(RED("No editions in the DB yet."))
                return 0
            _print_edition_list(editions)
            try:
                raw = input(BOLD("> ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if raw in ("q", "quit", ""):
                break
            try:
                idx = int(raw)
            except ValueError:
                print(RED(f"  Not a number: '{raw}'"))
                continue
            if idx < 1 or idx > len(editions):
                print(RED(f"  Out of range (1–{len(editions)})"))
                continue
            chosen_edition_id = int(editions[idx - 1]["id"])
            if _inspect_edition(db, chosen_edition_id, project_root):
                changed_anywhere = True

    if changed_anywhere:
        _maybe_republish(project_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
