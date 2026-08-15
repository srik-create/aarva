# Session plan — trend adds should auto-approve so they survive Stage 7 rebuilds

Small direct fix. No full spec.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

---

## The bug (diagnosis)

**Symptom (2026-08-15):** operator ran a normal editing session for
today's daily edition (edition_date=2026-08-15). Timeline:

1. `python -m aarva.daily` (full pipeline through Stage 7).
2. `python -m aarva.daily --stage 0` (curation crawl).
3. `python -m aarva.review` — picked 2 trend articles (`t17a t53a`
   → matched articles 8281 "cricket" and 5223 "September"),
   approved 3 regulars, rejected other regulars.
4. Re-ran `--stage 7` to refill empty slots (normal
   iterative-review workflow — happens most days).
5. Ran crosscut detect + build.
6. Ran stages 8, 9, 10, publish.

**Both trend articles were absent from the published edition.**

Verified against main DB:

- `trend_hits` shows both trends with `operator_action='added'`,
  `resolved_at=2026-08-15 08:49:35` (article 8281 and article 5223).
- Today's `edition_pieces` contains 5 pieces (all pipeline
  regulars, all `review_status='approved'`). NEITHER 8281 nor 5223
  appears.

**Root cause:** `aarva/stages/stage_7_assemble.py:842-846` —

```sql
DELETE FROM edition_pieces
 WHERE edition_id = ?
   AND review_status != 'approved'
```

Stage 7's rebuild wipes ANY piece with `review_status != 'approved'`.
`_apply_trend_decisions` at `aarva/review.py:734` calls
`add_article_to_todays_edition(db, article_id, slot=slot)`, which
inserts as `review_status='proposed'` per `edition_ops.py:75-83`.

So the sequence in step 4 nukes the two trend-added pieces because
they were `'proposed'` at that moment, waiting for a second review
pass that never ran (and shouldn't need to). The 5 already-approved
regulars survived because they'd been marked `'approved'` in step 3's
review.

Yesterday (2026-08-14) probably worked because the operator did NOT
re-run Stage 7 between the trend-add and Stages 8-10 — or because a
second review pass happened to approve the trend piece before Stage 7
re-ran. Today's timing differed.

---

## The fix

Trend adds should land as `review_status='approved'`, not
`'proposed'`. The `tNa` keystroke in the review CLI is already the
explicit operator decision — the propose-then-approve dance is
redundant for trend adds (unlike pipeline slot-picks, where the
review pass is where the operator first sees the piece).

**Concrete change — two files:**

### 1. `aarva/services/edition_ops.py`

Extend `add_article_to_todays_edition` signature to accept a
`review_status` parameter, default `'proposed'` for backwards
compatibility:

```python
def add_article_to_todays_edition(
    db: Database,
    article_id: int,
    slot: str = "manual_addition",
    position: int | None = None,
    review_status: str = "proposed",   # NEW
) -> AddResult:
    """... existing docstring ...

    review_status defaults to 'proposed' — the piece then goes through
    the ordinary review pass. Callers where the add IS itself the
    operator's approval (trend adds via `python -m aarva.review`'s
    `tNa`) pass 'approved' explicitly so the piece survives Stage 7
    rebuilds without a second review pass.
    """
```

Change the INSERT at line 75-83 to use the parameter:

```python
conn.execute(
    """
    INSERT INTO edition_pieces
        (edition_id, article_id, slot, position,
         hook, contextualisation, audio_url, review_status)
    VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)
    """,
    (edition_id, article_id, slot, position, review_status),
)
```

**Do NOT change** the callers in `aarva/search.py` or
`aarva/ingest_url.py` — those flows pre-date this decision and their
review-then-approve posture is intentional. Only the trend-add caller
opts into `'approved'`.

### 2. `aarva/review.py`

`_apply_trend_decisions` at line 734: pass `review_status='approved'`
explicitly:

```python
slot = "delight" if item.matched_jtbd == "delight" else "bonus"
result = add_article_to_todays_edition(
    db, article_id, slot=slot, review_status="approved",
)
```

---

## Rationale for approved-not-proposed on trend adds

Trend adds are structurally different from pipeline slot-picks:

- **Pipeline picks** (Stage 7's normal output) are the AI's proposal;
  the review pass is where the operator first sees them and decides.
  Propose → review → approve is the ordinary UX.
- **Trend adds** are the operator's explicit gesture (`tNa`) after
  seeing the trend's semantic match with a concrete matched article.
  The trend-add IS the approval decision — there's nothing more for
  a second review pass to add. The current propose-then-approve
  requires a second review invocation for no editorial benefit.

Same argument the drop-vs-reject design already uses at
`docs/session_plan_review_cli_polish.md`'s Fix 2: approved pieces
stay frozen through Stage 7 rebuilds because the operator's
approval was the intentional gate.

---

## Verification

1. **Unit test / integration test:** simulate the failing 2026-08-15
   flow — insert a trend match, apply `tNa`, re-run Stage 7 against
   the same edition. Confirm the trend-added piece survives the
   rebuild. Add to `aarva/tests/test_review.py` (or wherever
   `_apply_trend_decisions` is currently tested).
2. **Backward-compat:** confirm existing callers of
   `add_article_to_todays_edition` (`aarva/search.py`'s
   `--add-to-edition` / `--add` and `aarva/ingest_url.py`'s
   `--add-to-edition`) still insert as `'proposed'` — no signature
   break, no behavior change for those code paths.
3. **The `tNi` case (fallback URL ingest):** verify this path also
   auto-approves. `_apply_trend_decisions` at line 714-721 sets
   `article_id = _ingest_one(...)`, then falls through to the same
   `add_article_to_todays_edition(...)` call at line 734. So the
   `review_status='approved'` fix covers `tNi` too, no separate change
   needed. Add a test for this case explicitly.
4. **Real end-to-end (optional but valuable):** simulate the exact
   2026-08-15 timeline — Stage 7, trend-add via review, re-run Stage
   7, Stages 8/9/10. Confirm trend articles appear in the final
   `edition_pieces` after Stage 10.

---

## Roadmap

Rule 17a: bug fix, not a feature ship. Add a "Recently completed"
entry in `docs/roadmap.md` at merge time under 2026-08-15+ (whichever
date the fix ships). Reference this doc + the 2026-08-13 trend-signal
entry as the parent feature this patches.

---

## Rules verified in this handoff

- **AGENTS.md rule 4** (material trade-off pre-approval): user signed
  off 2026-08-15 in the conversation that produced this handoff —
  option A locked ("that. and may be option A is the best.").
- **AGENTS.md rule 17e** (cite-the-source discipline): every code
  reference cites file+line — verify against the current tree before
  implementing (`git fetch origin main` first).
- **AGENTS.md rule 17c** (STATUS-line discipline): the parent spec
  `docs/session_plan_trend_signal_for_delight.md` is STATUS: Shipped
  2026-08-13. This is a follow-up doc, not a rewrite of that spec's
  decisions.
- **AGENTS.md rule 20a** (Claude Code git protocol): Claude Code
  commits + pushes + opens PR directly, user gives explicit "merge it"
  before merge.
