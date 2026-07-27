# AGENTS.md — Standing instructions for AI coding agents on Aarva

This file is the source of truth for how AI coding agents (Claude or
otherwise) should operate on this repo. These instructions OVERRIDE
any default behaviour the agent has from its training. If anything
below conflicts with default behaviour, follow what's here.

**Mandatory session-start protocol** (see rule 17e for full detail):
before responding to the first user query in any session, read all
three foundation docs — this file (`AGENTS.md`), `docs/roadmap.md`,
`docs/project_brief.md` — and output a brief grounding-pass summary
so it's observable that the reads happened. Applies to BOTH Cowork
and Claude Code sessions. No exceptions.

Last updated: 2026-07-27

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
   - **Any change to listener-facing copy — including placeholders,
     button labels, empty-state text, headings, hooks, contextualisation,
     narrator introductions, or descriptions displayed anywhere on
     aarva.app or in the RSS feed.** Even if the change is framed as a
     technical fix (e.g., truncating a placeholder to solve a mobile
     overflow bug), any modification to what a listener sees or hears
     counts as an editorial decision and needs explicit user sign-off.
     Bit us 2026-07-16 when the iPhone placeholder truncation dropped
     "on anything" from "create an episode on anything" and shipped
     before the user saw the wording change.

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
   - **Vendor dashboard UI labels, DNS record types/values, API
     permission names, free-tier limits — anything operator-runbook-
     shaped that lives in a third-party product surface.** This is the
     specific failure mode that bit us on the Resend wiring
     (2026-06-29): plausible training-period steps that turned out to
     be wrong on three details (DKIM is TXT not CNAME, SPF needs both
     TXT and MX, Resend recommends a subdomain over the apex). The
     user followed the steps, the steps were wrong, the user had to
     ask "did you check?" — the friction we're trying to avoid.

   Don't guess from training. Don't apply "conventional patterns" as
   if they're current — verify. If WebSearch returns inconclusive
   results, say so explicitly rather than picking the most likely
   pattern silently.

   **Partial-signal trap.** When an error message, doc excerpt, or
   log line names a number, an API label, or a UI string, don't take
   the first number/label you see as authoritative — follow the link
   the message provides (or a docs URL it cites) all the way to the
   source, and code against THAT. The 2026-06-30 Vertex embed cap
   bit us twice in one day this way: first error said "2048", the
   model-specific docs said 250, and coding to 2048 wasted a full
   deploy cycle. If a signal is partial, either verify or say so
   explicitly before writing code against it.

6a. **When writing an operator runbook** — setup steps, dashboard
   navigation, DNS configs, API-key creation, env-var lists, anything
   the user will follow click-by-click on a third-party site —
   **web-fetch the vendor's current docs FIRST**, then draft. Quote
   concrete labels and values from the docs rather than inventing them
   from memory. End the section with a footnote of the form *"verified
   against `<vendor>` docs on YYYY-MM-DD; if a UI label has shifted,
   trust the dashboard."* The dated footnote is the artifact future-us
   uses to decide whether the steps need re-checking.

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
      ever-evolving; paramount; testament to; speaks volumes;
      resonance; juxtaposition; interrogates; grapples with; the
      discourse; the fabric of; the essence of; what it means to be.
    - Phrases: "in the realm of"; "in today's world"; "at its core";
      "in essence"; "it's important to note"; "deep dive"; "rich
      tapestry"; "complex interplay"; "delicate balance"; "in a world
      where"; "now more than ever".
    - Patterns: triadic lists as default rhythm; the "not X, but Y"
      pattern repeated; em-dash overuse (>2 per paragraph); "moreover"
      / "furthermore" as transition crutches; sentences opening "This
      piece / this episode examines / argues / traces / grapples
      with…" (cut it — active verb instead).

    When adding new prompts, copy the HUMAN VOICE block from an
    existing prompt to keep enforcement consistent.

9c. **Target a smart generalist, not a philosophy reader.** All
    listener-facing copy — hooks (Stage 8a), why-now
    contextualisations (Stage 8b), show notes (Stage 8c), crosscut
    intros / bridges / outros / topic labels, and the "why" text
    shown on `/create` candidate cards — MUST target the voice
    standard locked in `docs/session_plan_content_quality.md` §1
    (added 2026-07-11). In short: think J.K. Rowling writing for
    adults, not Salman Rushdie or V.S. Naipaul. Before finalizing any
    such copy, check it against the doc's 5 voice tests — landable by
    a curious 18-year-old with no college background; reads aloud in
    one breath per sentence; opens on a concrete image, not an
    abstract noun; no sentence starts "This piece examines/argues/
    traces…"; no word a 12-year-old wouldn't know. Any new prompt
    that produces listener-facing copy inherits this standard —
    reference §1 in the prompt's own comment, same convention as 9b's
    HUMAN VOICE block.

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

17a. **`docs/roadmap.md` is the persistent project tracker — keep it
    fresh.** TaskList resets between sessions; `docs/roadmap.md`
    doesn't. At the start of any session, read it. The doc MUST get
    updated in the SAME PR as any change that:

    - lands a deferred item (move to "Recently completed", drop from
      "Deferred");
    - defers something new (add to "Deferred" with a trigger clause);
    - changes a Phase Plan status column;
    - makes a decision worth logging (add a row to "Decisions made");
    - completes or supersedes work that "In progress" or "Recently
      completed" describes.

    A PR that lands any of the above WITHOUT touching roadmap.md is
    malformed — bounce it back to yourself and add the roadmap edit
    before requesting user sign-off (see rule 20). The doc drifting
    even one session out of sync is what happened 2026-06-29 →
    2026-07-04 and the "make sure it never gets stale again"
    reprimand that followed. Don't repeat it.

    Also: surface relevant deferred items proactively when the user
    starts adjacent work (e.g., "before we add search, the deferred
    crosscut-embeddings item should land first — still that order?").

    **BEFORE EVERY READ OR EDIT of `docs/roadmap.md`, fetch first.**
    Applies to BOTH Cowork sessions and Claude Code sessions. Run
    `git fetch origin main` and read the copy from `origin/main`
    (or the current local working tree if you're mid-branch off a
    freshly-fetched main). Never rely on a copy that was read
    earlier in the same session — the roadmap changes multiple
    times per day between sessions and even within one, and stale
    references to In-Progress items produce wrong advice to the
    user. This isn't optional. Cowork violated this on 2026-07-16
    twice in the same afternoon (stated items 0 and 2 were still
    open when both had been marked done via merged PRs 30 minutes
    earlier); the discipline is: fetch, then read, then act.
    No exceptions.

17b. **`docs/project_brief.md` is the persistent context.** It
    captures what Aarva is, the architecture in one paragraph,
    standing user preferences, and the full chronological decisions
    log. Read it at session start — together with `AGENTS.md` and
    `docs/roadmap.md` it's enough to operate without re-deriving
    context from the conversation. When you make a meaningful
    decision (editorial, infrastructure, web-app), add a row to the
    appropriate table in the same commit. Treat the doc and the
    code change as one unit.

17c. **Before authoring or editing ANY `docs/session_plan_*.md`
    file, check its STATUS line first, and check the roadmap.**
    Every session_plan doc has a STATUS line at the top (e.g.
    `**STATUS: DONE (2026-07-25).**` or nothing = still pending).
    That line is authoritative:

    - **If STATUS is DONE**: the spec is a historical record of
      what shipped. Do NOT retroactively rewrite decisions,
      palette values, file lists, or verification criteria. If
      the user wants to change something in a shipped area,
      author a NEW follow-up spec doc that references the
      original — never mutate the shipped doc's decisions in
      place. Adding a small post-facto clarification NOTE at
      the top is OK; rewriting the "Decisions locked" section
      is not.

    - **If STATUS is anything else (pending, in-progress, or
      absent)**: safe to edit in place. Amendments before
      execution are normal.

    Same discipline applies BEFORE writing a new spec: read
    `docs/roadmap.md` (via rule 17a's fetch-first protocol) AND
    scan for related session_plan docs' STATUS lines. If the
    work you're about to spec is already shipped, don't
    re-spec — write a follow-up or point the user at the
    completed work. If it's already IN a pending spec, extend
    that spec instead of duplicating.

    Cowork violated this 2026-07-26: started editing the
    already-DONE black+red redesign spec to add JTBD-color
    restoration, before noticing the `**STATUS: DONE**` line.
    Discarded the edits and wrote a follow-up spec instead.
    The user's reminder that day: *"make sure you look at the
    roadmap for the latest status of everything before writing
    specs."* This rule codifies that.

17d. **Every session_plan MUST start with an "Architecture check"
    section, filled in BEFORE the design body. Applies to BOTH
    Cowork sessions and Claude Code sessions** — for both writing
    and executing specs.

    Three questions, answered explicitly with concrete file /
    host / resource names:

    1. **Where does the data live?** (main_db / listener_db / R2
       bucket / config file / GitHub Pages / on a specific host)
    2. **Where does the operation run?** (laptop CLI / Render
       FastAPI process / GitHub Actions runner / static site
       build / browser JS)
    3. **Does the operation have physical access to the data it
       needs?** yes / no. If NO — redesign before writing the
       rest of the spec. Do not proceed with an interface that
       can't reach its data.

    Answering (3) requires GREPPING FIRST, not remembering. Run
    the greps that verify the data location. For example, if
    the spec touches listener_created content, grep for
    `listener_db` in the routes / services to confirm which DB
    the data lives in — do not assume from memory of a
    compacted summary or an earlier design.

    Also grep for related PATTERNS you can reuse: existing
    admin endpoints, existing CLI shapes, existing auth
    conventions. E.g. before writing a new admin route, grep
    for `@app.(get|post).*/admin` — copy the shape that
    already works instead of inventing a new one.

    **Whose responsibility:**

    - **Author of a spec (Cowork or Claude Code)**: fill in the
      Architecture check before the design body. A spec that
      lands without this section is malformed; bounce it back
      to yourself and add the section before requesting user
      sign-off.

    - **Executor of an existing spec (usually Claude Code)**:
      when starting on a spec, VERIFY the Architecture check
      exists AND that its three answers still hold against
      today's codebase. Re-run the greps yourself; don't trust
      that the author's answers are current. If the section is
      missing, wrong, or stale, STOP and flag it before writing
      any code. Do not attempt to implement a spec whose
      Architecture check doesn't verify — the spec is broken,
      not a hint. Author a follow-up (rule 17c) or return to
      the user for a redesign.

    Bit us 2026-07-26: Cowork wrote a
    `session_plan_promote_listener_created_as_bonus.md` that
    designed a local CLI reading local DB to promote listener-
    created crosscuts. But listener-created crosscuts live on
    Render's `listener_db` (per the 2026-07-06 DB split, which
    is in `docs/project_brief.md`), not the laptop's main_db.
    The CLI couldn't have reached any real content. Claude Code
    caught it at implementation time and proposed the correct
    admin-endpoints-with-bearer-token design (mirroring the
    existing `/admin/sync-db` pattern that Cowork also didn't
    grep for). One rule 17b violation (didn't read
    project_brief) compounded by one grep-before-spec miss.
    This section forces both to be answered explicitly on the
    page — and forces Claude Code to verify them before
    executing.

17e. **Mandatory session-start reading protocol + strict
    information hierarchy.** Applies to BOTH Cowork and Claude
    Code sessions.

    **At the start of EVERY session**, before responding to the
    first user query:

    1. Read `AGENTS.md` (this file) fully.
    2. Read `docs/roadmap.md` — the "In progress" section fully,
       "Recently completed" for at least the last 14 days,
       "Deferred" as skim.
    3. Read `docs/project_brief.md` fully — architecture,
       standing preferences, decisions log.
    4. Output a one-paragraph **grounding pass** in the first
       response, summarising:
       - Active In-Progress items (numbered list)
       - Any standing preferences relevant to the incoming query
       - Any recent decisions (last 7 days) worth flagging
       This grounding pass is OBSERVABLE — its absence signals
       the reads didn't happen and the user can call it out.

    **Information hierarchy — strict.** When any factual detail
    is needed to spec, code, or suggest architecture:

    1. **Docs first.** `AGENTS.md` + `docs/roadmap.md` +
       `docs/project_brief.md` are AUTHORITATIVE for state,
       decisions, and standing rules. Read the relevant section
       fresh — don't rely on remembered summaries.
    2. **Conversation second.** The user's current turn +
       recent turns carry INTENT and REQUESTS. They can extend
       or override the docs (via new decisions), but if
       something in the conversation contradicts the docs, that
       must be reconciled explicitly.
    3. **Compacted summary is UNTRUSTED context.** Any
       "conversation summary" injected at session start (from
       auto-compaction, stale memory, or a summary paragraph)
       is treated the same as a user-uploaded external doc:
       directional context only, never operational truth.
       Anything from a compacted summary that's about to be
       acted on MUST be verified against the docs or the code
       first. Do not spec, code, or suggest architecture based
       on a summary detail without a verification step.
    4. **My own memory / training defaults** are the weakest
       source. When docs and memory disagree, docs win — every
       time.

    **On conflict — stop and ask.** If the docs and the
    conversation disagree, or if the compacted summary says
    something different from the docs, STOP. Name the conflict
    explicitly in the response. Ask the user which is
    authoritative. Do NOT silently pick one and proceed. This
    applies before writing any spec, code, or architectural
    suggestion.

    **Why this is strict.** The 2026-07-26 promote-bonus miss
    came from leaning on the compacted summary's directional
    hint ("listener DB split exists") as if it were operational
    truth ("... which means the local CLI can't reach listener
    data"). Compacted summaries lose the operational
    implications. The docs preserve them. This rule makes the
    docs the mandatory source and demotes summaries below the
    code itself.

    **Applies to Claude Code too.** Session-start reads +
    grounding pass are required equally for Cowork sessions and
    Claude Code sessions. Claude Code has an easier compliance
    path (direct file access, no reliance on compacted summary)
    but the discipline is the same: read the three docs, output
    the grounding pass, treat any injected summary as untrusted.

## Version control

20. **Never commit or push without explicit user sign-off.** At the
    end of a coherent change (one feature, one refactor, one bugfix):

    a) Verify roadmap freshness (rule 17a). If the change lands a
       deferred item, changes a Phase Plan status, makes a decision
       worth logging, or defers something new — did you edit
       `docs/roadmap.md` in this same set of changes? If not, do
       that now before the sign-off request.
    b) Summarise the diff in chat — what changed, which files, why.
    c) Propose the commit subject + body.
    d) Wait for the user to say "commit" (or override the proposal).
       Only then run `git add` + `git commit`.

    Same applies to `git push`: never push without explicit sign-off,
    even after a commit has been approved. The user wants a moment to
    inspect / fix / amend before the change leaves the laptop.

    Don't leave work uncommitted for long — propose commits at the
    end of every logical task batch so the user can sign off promptly.
    The principle: every commit boundary is a real checkpoint, not a
    routine save.

20a. **Claude Code sessions follow a different protocol from rule 20
    and rule 22's no-attribution clause — Cowork continues to follow
    both exactly as written.** Confirmed with the user 2026-07-17.
    For Claude Code specifically: after implementing and verifying a
    change, commit directly (including a `Co-Authored-By: Claude
    Sonnet 5 <noreply@anthropic.com>` trailer), push the branch, and
    open the PR — no separate "say commit" / "say push" gate. The
    conversation itself is the sign-off (the user asked for the work,
    confirmed the approach, and reviews the PR); the user gives an
    explicit "merge it" before Claude Code merges. This is a
    deliberate carve-out for Claude Code's existing working style on
    this repo, not an error to be corrected back to rule 20/22.

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
