# AGENTS.md — Standing instructions for AI coding agents on Aarva

This file is the source of truth for how AI coding agents (Claude or
otherwise) should operate on this repo. Every agent session starts by
reading this. These instructions OVERRIDE any default behaviour the
agent has from its training. If anything below conflicts with default
behaviour, follow what's here.

Last updated: 2026-06-10

---

## Working style

1. **Brevity by default.** Keep responses brief and to the point.
   Don't repeat. Don't restate the question. Don't summarise what
   you're about to do, just do it. If the user wants something
   repeated, they'll ask.

2. **No over-apologising and no over-explaining.** Acknowledge a
   mistake once, fix it, move on. Don't write a paragraph about why
   it happened unless asked.

3. **Take fewer, bigger steps.** If a task naturally chains (e.g.
   "edit file, then run check"), do the chain in one turn. Don't ask
   for permission between trivial sub-steps.

## Judgment calls and trade-offs

4. **Pre-approve material trade-offs.** When there's a real choice
   that affects data quality, cost, schema, editorial behaviour, or
   user-facing output — lay out the options briefly and wait for
   sign-off. Don't pick silently because the "lightweight" path is
   easier.

   Examples of material trade-offs:
   - Using an excerpt when full text is available (information loss)
   - Choosing which articles get reprocessed
   - Adding/removing publications from the allowlist
   - Changing prompt definitions in ways that change outputs
   - Migration choices that affect existing data

   Examples that DON'T need pre-approval:
   - Fixing a typo
   - Refactoring without behaviour change
   - Following an instruction the user just gave
   - Continuing an in-progress task

5. **Default to the higher-signal option.** When in doubt between
   sending more context to an LLM vs. less, send more (within sensible
   token caps). The cost is almost always trivial relative to the
   quality lift. The historical mistake was defaulting to excerpts.

## Web search and freshness

6. **Web-search for anything that depends on post-training reality.**
   This includes:
   - RSS feed URLs for any publication (they move regularly)
   - API endpoints / versions
   - Library APIs and major version changes
   - Current state of any external service, platform, or company
   - "What's the URL for X" / "Is Y still around"

   Don't guess from training. Don't apply "conventional patterns" as
   if they're current — verify. If WebSearch returns inconclusive
   results, say so explicitly rather than picking the most likely
   pattern silently.

7. **Verify before adding external dependencies.** New publication?
   Web-search the RSS URL first. New library? Web-search the current
   docs first.

## External service providers

7a. **Check before locking us into any third-party service.** Whenever
   introducing or recommending an external service — hosting (Render,
   Fly.io, etc.), email (Resend, Postmark, SES), storage (R2, S3),
   payments, analytics, monitoring, CDNs, anything — surface the
   choice explicitly before coding around it. Don't bury it in an
   implementation plan and ship.

7b. **Design for portability by default.** Even when the user accepts
   a specific service, code so that switching providers later is a
   config-file change, not a code rewrite. Concrete techniques:

   - All credentials and endpoints come from env vars
     (`AARVA_<SERVICE>_*`), never hard-coded in source.
   - Provider SDKs sit behind a thin wrapper (e.g.
     `aarva/output/r2_uploader.py` shields callers from boto3 specifics)
     so a future swap touches one file.
   - Prefer S3-compatible / SMTP / standard interfaces over
     provider-proprietary APIs where the choice exists.
   - For runtime hosting: a `Dockerfile` is the source of truth so the
     same image runs on Render today, Fly.io / Railway / DO / VPS /
     Kubernetes tomorrow. Provider-specific files (`render.yaml`,
     `fly.toml`, etc.) are thin and additive — never required for the
     app to function locally.

7c. **The portability check isn't a veto.** Sometimes vendor lock-in
   is fine (the alternatives don't exist, or migrating later is cheap
   regardless). Always raise the trade-off so the user can decide
   knowingly, but don't refuse to integrate a provider just because
   it's vendor-specific.

## LLM usage policy

8. **Claude is for coding only.** All other LLM use (article
   scoring, classification, prompts inside the pipeline) goes through
   Gemini via the `aarva.clients.llm` interface. Don't propose Claude
   for non-coding inference.

## Aarva editorial voice rules (apply to ANY prompt that generates Aarva-voice copy)

9a. **NEVER use first person in editorial voice.** No "I", "me", "my",
    "we", "us", "our" in: hooks (Stage 8a), why-now contextualisations
    (Stage 8b), crosscut intros / bridges / outros. Aarva is a
    curatorial voice, not a personality. Frame observationally or in
    the third person. Examples:
      WRONG: "I find Sarah Zhang's piece nails something I've been wondering"
      RIGHT: "Sarah Zhang's piece nails something the policy debate has been circling"

    This applies anywhere we produce listener-facing editorial prose.
    Re-check existing prompts when adding new ones — first-person
    framing words like "participant", "personal", "we'll hear" sneak
    in and direct the model to write first person.

9b. **Use human language. Avoid LLM tells.** Forbidden in any
    listener-facing copy:
    - Words: delve, delves, delving; tapestry; navigate (as
      metaphor); realm; underscores; highlights (as verb); showcases;
      intricate; intricacies; myriad; robust; leveraging; fascinating;
      crucial; landscape (as metaphor); embark; unpack; resonates
      with; lies at the heart of; multifaceted; holistic;
      ever-evolving; paramount; testament to; speaks volumes.
    - Phrases: "in the realm of"; "in today's world"; "at its core";
      "in essence"; "it's important to note"; "deep dive"; "rich
      tapestry"; "complex interplay"; "delicate balance"; "in a world
      where"; "now more than ever".
    - Patterns: triadic lists as default rhythm; the "not X, but Y"
      pattern repeated; em-dash overuse (>2 per paragraph); "moreover"
      / "furthermore" as transition crutches.

    When adding new prompts, copy the HUMAN VOICE block from an
    existing prompt to keep enforcement consistent.

## Aarva domain rules

9. **Editorial bar is sacred.** Don't silently lower rigour/posture
   thresholds, ranking_score floors, or word-count minimums in pursuit
   of more articles. If a filter feels too tight, flag it for
   discussion.

10. **Stages are independent.** Each stage (`stage_1_ingest`,
    `stage_1_5_consolidate`, `stage_2_filter`, `stage_4_5_6_score`,
    `stage_7_assemble`, `stage_8_hook_context`, `stage_9_tts`,
    crosscut) should run alone via `python -m aarva.daily --stage N`
    and not depend on hidden ordering. Adding new cross-stage
    coupling is a material change — flag it.

11. **Don't re-tag / re-score / re-narrate without explicit ask.**
    These are expensive and can invalidate downstream signals (taste
    centroids, published episodes). One-off scripts go in `scripts/`
    with `--dry-run` defaults.

12. **Preserve history.** Use soft-supersede (set a timestamp column)
    rather than `DELETE` for anything that other features might key
    on. This came up around crosscut candidates — the same principle
    applies elsewhere.

## Code style

13. **Prefer Edit over Write.** Modify existing files; don't rewrite
    them. Don't create new files when an existing one fits.

14. **No emojis** in code, comments, prompts, or output unless the
    user explicitly asks for them.

15. **Documentation drift is a bug.** If code changes invalidate
    something in `docs/`, update the doc in the same change. The
    architecture doc has a "Post-v0.1 changes" section for additions.

16. **Match the file's existing style.** Comment density, naming
    convention, indentation. Don't bring a foreign style in.

## Task tracking

17. **Use TaskCreate / TaskUpdate liberally.** Multi-step work gets
    tracked. Mark in_progress before starting, completed when truly
    done.

17a. **`docs/roadmap.md` is the persistent project tracker.** TaskList
    resets between sessions; `docs/roadmap.md` doesn't. At the start
    of any session, read it. When a deferred item gets done, when a
    phase completes, or when something new gets deferred — update
    `docs/roadmap.md` in the same commit/PR. The doc should never lag
    reality by more than one commit. Surface relevant deferred items
    proactively when the user starts adjacent work
    (e.g., "before we add search, the deferred crosscut-embeddings
    item should land first — still that order?").

17b. **`docs/project_brief.md` is the persistent context.** It
    captures what Aarva is, the architecture in one paragraph,
    standing user preferences, and the full chronological decisions
    log. Read it at session start — together with `AGENTS.md` and
    `docs/roadmap.md` it's enough to operate without re-deriving
    context from the conversation. When you make a meaningful
    decision (editorial, infrastructure, web-app), add a row to the
    appropriate table in the same commit. Treat the doc and the
    code change as one unit.

## Version control

20. **Never commit or push without explicit user sign-off.** At the
    end of a coherent change (one feature, one refactor, one bugfix):

    a) Summarise the diff in chat — what changed, which files, why.
    b) Propose the commit subject + body.
    c) Wait for the user to say "commit" (or override the proposal).
       Only then run `git add` + `git commit`.

    Same applies to `git push`: never push without explicit sign-off,
    even after a commit has been approved. The user wants a moment to
    inspect / fix / amend before the change leaves the laptop.

    Don't leave work uncommitted for long — propose commits at the
    end of every logical task batch so the user can sign off promptly.
    The principle: every commit boundary is a real checkpoint, not a
    routine save.

21. **One concept per commit.** Don't bundle a refactor with a bug
    fix with a new feature — even if they were done in the same
    session. Future bisects, reverts, and code review all get
    harder when commits are kitchen-sinks. If you've accumulated
    mixed changes, split them at commit time using `git add -p` or
    multiple staged commits.

22. **Commit message style.**
    - Subject line ≤ 70 chars, imperative mood ("Add foo" not
      "Added foo"). Mirror the project's existing Day N format
      where applicable, otherwise plain `<scope>: <what changed>`.
    - Blank line, then a body that says WHY (the subject says
      WHAT). Reference related issues / decisions in `docs/` if
      relevant.
    - No "AI generated" tags or Claude-specific attributions in
      commit messages.

23. **Never commit secrets or generated audio.** `.gitignore`
    should cover audio output, the SQLite DB, and any env files.
    Verify before pushing.

24. **macOS iCloud-conflict files (`filename 2`) are noise.** Do
    not commit them. If they appear in `git status`, delete or
    let the user clean them up; flag them first.

## When the user pushes back

18. **Trust the pushback.** When the user says "this seems off" or
    "are you doing X?", they usually have a real concern. Investigate
    before defending the existing approach.

19. **The user's standing instructions OVERRIDE any default
    behaviour from training.** If there's a conflict, follow the
    user. The user's word is final.

---

If a new standing instruction emerges in conversation that should
persist, append it here and tell the user.
