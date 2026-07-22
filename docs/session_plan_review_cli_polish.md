**STATUS: DONE (2026-07-18).** Both fixes shipped in one PR. See
`docs/roadmap.md`'s 2026-07-18 "Recently completed" entry for the
full writeup. One addition beyond this spec: the blanket shortcuts
(`all-a`/`all-r`/blank-Enter) were restricted to only sweep pieces
that were `'proposed'` at load time, so making approved pieces
visible/indexed (Fix 2) can't let a blanket `all-r` accidentally
reject an already-frozen approved piece — not explicitly called out
in this doc, added to preserve the existing "approved pieces stay
frozen" invariant.

---

# Session plan — review CLI polish (drop-then-resurface fix + un-approve)

Written by Cowork for the next Claude Code session (2026-07-18+).
Two small independent gaps in `python -m aarva.review`, spec'd
together because they touch the same file and can ship as one PR.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

Two things the user hit on 2026-07-18 while doing the daily review:

1. **Dropped articles resurface within the SAME edition.** When
   the user hits `Nd` to drop a piece, the current code
   (`aarva/review.py::_apply_decisions`, lines 462-480) removes
   the piece from `edition_pieces`, adds the SLOT NAME to
   `editions.dropped_slots`, and resets the article's status to
   `'scored'`. Stage 7's refill then correctly skips the dropped
   slot — but the dropped **article itself** is now fully eligible
   again, and Stage 7 can pick it into a DIFFERENT slot in the
   same edition. The article reappears in the reviewer's next
   round, which is not the user's intent.
2. **Once a piece is approved, the review CLI has no way to
   un-approve it.** `_load_proposed` (line 108-123) only reads
   `review_status = 'proposed'` rows, so approved pieces are
   invisible to the CLI. If the user changes their mind about an
   approved piece before the edition is finalised, the only
   current fix is a manual SQL update to flip status back to
   `'proposed'`.

Both fixes live in `aarva/review.py` + a small schema addition.
No changes downstream of Stage 7 needed.

---

## Fix 1 — Drop excludes the article from THIS edition

### Decision locked (with user, 2026-07-18)

- Same edition: dropped article is **excluded entirely** for
  this edition (all slots, not just the one it was dropped from).
- Future editions: **still eligible** — matches the existing
  design comment in `review.py` at lines 464-466. Drop = "not
  right now, not in this edition" — nothing stronger.

### Schema

New column on `editions`:

```sql
ALTER TABLE editions
  ADD COLUMN dropped_article_ids TEXT DEFAULT '[]';
-- JSON list of article_ids dropped from THIS edition. Parallels
-- dropped_slots (which lists slot names). NULL/absent on legacy
-- editions — treated as empty list.
```

Add to `aarva/db.py`:
- The `CREATE TABLE editions` definition (currently around line
  110-120)
- A one-shot migration in the same style as the existing
  `dropped_slots` migration (line ~400)
- The `editions`-copy path used by tests/backfills if applicable
  (line ~556 area — mirror the pattern used for `dropped_slots`)

### CLI change (`aarva/review.py`)

In `_apply_decisions`, the `action == "d"` branch (line 462-480):
after the existing delete + `dropped_slots.append(piece.slot)`,
also append `piece.article_id` to a new local list
`dropped_article_ids` loaded from the same editions row (mirror
the existing `dropped_slots` load at line 420).

Persist it in the `UPDATE editions SET …` at line 491-495 —
add `dropped_article_ids = ?` alongside the existing three
columns.

### Stage 7 change (`aarva/stages/stage_7_assemble.py`)

`_load_edition_overrides` (line 502-520) currently returns
`(extra, dropped, biases)`. Extend to also return
`dropped_article_ids` — a fourth item in the returned tuple.
Update the one caller (around line 968).

Then in Stage 7's candidate selection (the point where the
per-slot candidate pool is filtered), add a filter
`article.id NOT IN dropped_article_ids`. Concretely: pass the
set into whatever function builds the per-slot candidate list,
and skip any candidate whose id is in the set. Location: look
around the code that reads `edition_pieces` for already-approved
pieces (line 577+) and the pool-building call site near line 968.

### Verification

1. Set up an edition with N proposed pieces. Drop one via `Nd`.
   Re-run Stage 7. Confirm:
   - The dropped slot is not refilled (existing behaviour ✓).
   - The dropped article does NOT appear in any other slot of
     THIS edition.
2. Confirm the dropped article IS eligible for the NEXT day's
   edition (query `articles.status` = `'scored'`, run tomorrow's
   Stage 7, verify article can be picked).
3. Legacy edition rows with NULL `dropped_article_ids` don't
   break Stage 7 — the empty-list fallback fires.

---

## Fix 2 — Un-approve in the review CLI

### Goal

A `u` command that flips an approved piece back to
`review_status='proposed'` so the reviewer can then re-decide
(approve, reject, or drop). Only meaningful before the edition
is finalised.

### CLI UX

Add a new command form to the batch line syntax:

```
Nu       un-approve piece N (flip approved → proposed)
```

Same shape as `Na` / `Nr` / `Nd` — user types `3u` to un-approve
piece 3. But: **piece indices in the current CLI only cover
proposed pieces** (`_load_proposed` filters to `review_status =
'proposed'`), so approved pieces don't have an index visible to
the user.

Options considered:
- **A.** Show approved pieces in the CLI listing (with a distinct
  visual marker) and give them their own index range.
- **B.** Provide `u` as an out-of-band command that takes an
  article_id, e.g. `u:1234`.

**Recommendation: A.** Reviewers think in terms of "the edition
as it stands now" — showing approved pieces alongside proposed
ones (visibly marked, e.g. green ✓) makes the state of the
edition legible AND makes un-approve a natural extension of
the same numbered UX.

### Implementation sketch

- Rename `_load_proposed` → `_load_review_pieces` (or similar).
  Load BOTH `review_status = 'proposed'` AND
  `review_status = 'approved'` for the edition. Add
  `review_status` to the ProposedPiece dataclass (or rename it).
- Render both in the CLI listing. Approved pieces get a leading
  ✓ marker (or `[approved]` prefix — pick what reads best
  alongside the existing dim/coloured output).
- `_apply_decisions` gets a new `action == "u"` branch that
  simply runs:
  ```sql
  UPDATE edition_pieces SET review_status = 'proposed'
   WHERE edition_id = ? AND article_id = ?
  ```
  Also: if the piece had a bias set on approval (it doesn't
  today — approval CLEARS bias per line 435 — but be defensive),
  don't touch bias here.
- Help text update: add `Nu   un-approve piece N` to the help
  block around line 546.

### Verification

1. Approve a piece via `1a`. Confirm status = 'approved' in DB.
2. Re-run `python -m aarva.review` on the same edition. The
   approved piece is visible in the listing with a ✓ marker.
3. Run `1u` (or whatever its index now is). Confirm status
   flips back to 'proposed'.
4. Follow up with `1r` (reject) — the standard reject flow
   works, reason picker fires, article ends up in
   `edition_rejections`.

---

## Non-goals

- Do NOT extend un-approve to un-reject or un-drop. Rejects
  carry a reason (Phase 1 learning loop signal — don't discard);
  drops now carry same-edition exclusion (Fix 1 above — don't
  discard).
- Do NOT change future-edition behaviour of drops. Explicit
  scope from the user (2026-07-18).
- Do NOT retroactively add `dropped_article_ids` to legacy
  editions. Empty-list fallback covers them.

---

## Files that change

- `aarva/db.py` — schema addition + migration for
  `dropped_article_ids`.
- `aarva/review.py` — drop branch appends article_id; new `u`
  action; listing includes approved pieces with marker; help
  text.
- `aarva/stages/stage_7_assemble.py` — read the new column;
  filter candidates against it.
- `docs/roadmap.md` — after this PR merges, move item from
  In-Progress to Recently Completed (Claude Code owns this
  update per AGENTS.md rule 17).
