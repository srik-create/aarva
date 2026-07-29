**STATUS: Fixes A-E (all shipped 2026-07-29) treated symptoms of a
deeper architectural gap, found and fixed the same day as Fix F.**
The user reproduced overlap a THIRD time after Fixes A-E deployed,
this time with the back button confirmed as a required precondition.
That detail led to the actual root cause: no page sets `hx-history-elt`,
so htmx's history-cache save/restore (used on every back/forward
navigation once a page is cached) falls back to swapping the entire
`<body>` instead of `#main-content` — and htmx's `normalizeScriptTags`
forces every `<script>` tag in swapped content to re-execute. Since
the persistent-player `<script>`, the shared `<audio>` element, and
the mini-player all live inside `<body>` but outside `#main-content`,
every back/forward navigation was silently re-running the entire
player script as a brand-new, independent instance — with its own
audio element that auto-resumes from `sessionStorage` — while the
previous instance's element/listeners could still be attached or
referenced. **This is the real mechanism behind the original bug,
the "dissonance" between the two controls, and both "partial fix"
recurrences — Fixes A-E were all correct, real, worth keeping, but
none of them addressed this.** Fix F (below) is a one-line change
(`hx-history-elt` on `#main-content`) that stops history-cache
save/restore from ever touching the persistent player at all,
matching the architecture's original intent. Unlike every other fix
in this doc, this one's root mechanism is pure htmx/DOM behavior, not
an iOS-Safari-only quirk — it was verified deterministically in
headless Chromium (see Fix F's verification section), which is a
categorically stronger confirmation than anything else here. Real-
device confirmation from the user is still the final word, but this
is the first fix in the whole investigation that didn't need to lean
on "can't verify without a real iPhone."

---

# Session plan — fix web audio player dual-playback overlap

Written by Claude Code, 2026-07-29, following its own diagnosis-only
investigation earlier the same day
(`docs/diagnosis_audio_overlap_2026-07-29.md`). Per the user's
explicit instruction, this spec, the implementation, and the roadmap
update are all being done by Claude Code directly — no separate
Cowork spec-authoring step for this item.

---

## Architecture check

1. **Where does the data live?** N/A — this bug and its fix are
   entirely client-side JavaScript. No database row, R2 object, or
   config file is read or written by any of the fixes below.
2. **Where does the operation run?** Entirely in the listener's
   browser, inside the persistent-player `<script>` block already
   shipped in `aarva/server/templates/base.html:578-970` (byte-for-
   byte re-read this session to confirm current line numbers — see
   citations throughout). The FastAPI server only serves this
   template unchanged; no route or backend logic is touched.
3. **Does the operation have physical access to the data it needs?**
   Yes, trivially — every fix operates on the single `audio`
   variable (`base.html:584`, `document.getElementById('aarva-
   shared-audio')`) and DOM elements already in scope inside the same
   IIFE. No cross-boundary access question arises.

---

## Background

Full diagnosis: `docs/diagnosis_audio_overlap_2026-07-29.md` (and its
2026-07-29 UPDATE section). Summary of the two confirmed/likely
mechanisms this spec fixes:

1. **Confirmed, user-reproducible**: rapidly alternating taps between
   the in-page article play button and the mini-player bar's toggle
   button causes overlapping/dual audio. Mechanism: two independent,
   uncoordinated click handlers both call `audio.play()`/`audio.
   pause()` directly based on reading `audio.paused`, with no
   coordination around `play()`'s asynchronous, promise-based nature.
2. **Unrefuted, matches the original video evidence**: a second,
   independent execution of this same script (a second `Document`
   instance — most likely a bfcache-frozen sibling page) can coexist
   with the active one, each driving its own copy of the single
   shared `<audio>` element in its own DOM tree. `base.html` has no
   code path that pauses a page's own audio before it's backgrounded/
   frozen, and no code path that resyncs state if a page is restored
   from such a frozen state.

---

## Fix design

### Fix A — consolidate the two toggle handlers into one guarded function

**Problem sites**, both currently duplicate the same unguarded logic:

- `playTrack()`'s same-track branch, `base.html:733-738`:
  ```js
  if (current.src === src) {
    // Same track — toggle.
    if (audio.paused) audio.play().catch(function(){});
    else audio.pause();
    return;
  }
  ```
- `miniToggle`'s click handler, `base.html:796-800`:
  ```js
  miniToggle.addEventListener('click', function() {
    if (!audio.src) return;
    if (audio.paused) audio.play().catch(function(){});
    else audio.pause();
  });
  ```

**Change**: introduce one `togglePlayback()` function (placed near
`playTrack()`, before it's first referenced) that both call sites use
instead of duplicating the paused-check:

```js
// Guards against the rapid-alternating-tap race between the in-page
// play button and the mini-player toggle: play() is asynchronous and
// sets audio.paused = false synchronously, before playback actually
// starts natively. Calling pause() while that play() is still
// settling is a well-documented trigger for "The play() request was
// interrupted by a call to pause()" — swallowing that error (as the
// old code did) doesn't prevent the underlying native race, it just
// hides it. Waiting for the in-flight play() to settle before
// issuing pause() removes the overlap window entirely.
let pendingPlay = null;

function togglePlayback() {
  if (audio.paused) {
    pendingPlay = audio.play().catch(function() {});
  } else if (pendingPlay) {
    pendingPlay.then(function() { audio.pause(); });
    pendingPlay = null;
  } else {
    audio.pause();
  }
}
```

`playTrack()`'s same-track branch becomes:
```js
if (current.src === src) {
  togglePlayback();
  return;
}
```

`miniToggle`'s handler becomes:
```js
miniToggle.addEventListener('click', function() {
  if (!audio.src) return;
  togglePlayback();
});
```

Note: `pendingPlay` only needs to track the most recent `play()`
call — if `togglePlayback()` is called a third time while a
`pause()`-after-pending-play is queued, the queued `.then()` will
still fire (pause is idempotent-safe to call on an already-paused
element), and any newer play/pause request after that reads the
then-current `audio.paused` correctly since the queued pause already
ran by then in practice (single-threaded JS microtask ordering).

### Fix B — pause this document's own audio before it's backgrounded/frozen

**Problem site**, `base.html:942-943`:
```js
window.addEventListener('beforeunload', saveState);
window.addEventListener('pagehide',     saveState);
```
`pagehide` only saves state; it never stops playback. If this
document is about to be frozen into bfcache (or discarded outright),
its audio can keep playing/emitting independent of whatever page
becomes foreground next.

**Change**: extend the `pagehide` listener to also pause playback
before saving state:
```js
window.addEventListener('beforeunload', saveState);
window.addEventListener('pagehide', function() {
  if (!audio.paused) audio.pause();
  saveState();
});
```
`saveState()` (`base.html:656-670` per the existing implementation)
already persists `isPlaying` from `audio.paused` at call time, so
calling `audio.pause()` first means a resumed page correctly restores
into a paused state — matching the "silently stay paused, one tap
away" resume behavior the restore-on-load block already documents
(`base.html:959-961`'s comment).

### Fix C — resync state if a page is restored from bfcache

**Gap**: no `pageshow` listener exists anywhere in `base.html`
(confirmed by grep this session: `grep -n pageshow aarva/server/
templates/base.html` returns nothing before this change). A page
thawed from bfcache resumes exactly where its JS state was frozen,
which may now be stale relative to `sessionStorage` (e.g. if a
DIFFERENT, currently-foreground tab/page changed the shared playback
state in the meantime — not fully possible today since `sessionStorage`
is per-tab, but this guards against any future or edge-case
divergence, and costs nothing to add defensively).

**Change**: add near the other page-lifecycle listeners:
```js
window.addEventListener('pageshow', function(e) {
  if (e.persisted) {
    wireDataPlayers();
    updateUI();
  }
});
```
Reuses the existing `wireDataPlayers()` (`base.html:903-929`) and
`updateUI()` (`base.html:684-729`) functions already called after
every `htmx:afterSwap` — no new logic, just an additional trigger for
the same resync.

### Fix D — pause before `src` reassignment in `playTrack()`

**Problem site**, `base.html:739-741`:
```js
current = { src: src, title: title || '', link: link || '/today' };
audio.src = src;
audio.currentTime = 0;
```
No `audio.pause()` first, unlike the close button
(`base.html:842-844`: `audio.pause(); audio.removeAttribute('src');
audio.load();`), which does this correctly.

**Change**: add `audio.pause();` as the first line of this block,
matching the close button's existing pattern:
```js
audio.pause();
current = { src: src, title: title || '', link: link || '/today' };
audio.src = src;
audio.currentTime = 0;
```

---

## Fix E — full unload before loading a new track (added 2026-07-29, same day, after Fixes A-D were found incomplete)

**Evidence this was still missing**: the user reproduced overlap on a
real iPhone after Fixes A-D deployed
(`ScreenRecording_07-29-2026 14-52-51_1.mov`, 34.9s, downloaded and
independently re-analyzed this session). Frame-by-frame timing of
that recording: a track switch (tapping a different article's play
button while another track was mid-playback) at video t≈6s, and a
second track switch at video t≈12s — both timestamps the user
specifically flagged as when overlapping voices start. Spectrograms
of the recording (regenerated this session at
`/private/tmp/claude-501/-Users-srikant-Projects-Aarva/aac3d432-0e19-483c-a805-6dde8192d06f/scratchpad/audio_bug_diag2/spectrogram_full.png`
and a zoomed pass over the t=4-14s window at
`.../audio_bug_diag2/spec_4_14.png`) showed no sustained dense-overlap
signature of the kind the ORIGINAL bug videos showed. An instrumented
headless-Chromium reproduction of an equivalent rapid track-switch
sequence (script + raw 50ms-resolution trace saved at
`.../audio_bug_diag3/instrument_v2.py` and
`.../audio_bug_diag3/trace_v2.json`) found 0 divergent samples across
92 samples — the mini-player and in-page card stayed in lockstep the
entire time. This means the overlap isn't visible in any
JS-observable state (`audio.paused`/`currentTime`/`src`), which is
consistent with it occurring one layer below what JS can see: the
native decode/output buffer. (These are session-local scratch
artifacts, not committed to the repo — same convention as the
spectrogram citation in `docs/diagnosis_audio_overlap_2026-07-29.md`.)

**Problem site**, `base.html:759-762` (as shipped by Fixes A-D):
```js
audio.pause();
current = { src: src, title: title || '', link: link || '/today' };
audio.src = src;
audio.currentTime = 0;
```
A bare `pause()` followed directly by reassigning `src` stops JS from
reading the old resource, but per the HTML media element load
algorithm this is a lighter-weight reset than a full unload — it does
not guarantee already-decoded audio sitting in the browser's native
playback buffer is discarded before the new resource starts
producing output. On iOS Safari specifically, that leftover buffered
audio can keep emitting from the previous track while the new track's
audio also starts, audible as an overlap even though every JS-visible
signal (`audio.paused`, `audio.currentTime`, `current.src`) correctly
reflects only the NEW track throughout. This also explains why the
overlap compounds with each successive switch rather than appearing
once: each switch is an independent opportunity to leave a fresh
leftover buffer running.

**Change**: insert `audio.removeAttribute('src'); audio.load();`
between the `pause()` and the new `src` assignment — the same full
unload sequence the close button already uses correctly
(`base.html:842-844`, unchanged by this fix):
```js
audio.pause();
audio.removeAttribute('src');
audio.load();
current = { src: src, title: title || '', link: link || '/today' };
audio.src = src;
audio.currentTime = 0;
```
`removeAttribute('src')` + `load()` forces the media element's load
algorithm to run with NO current resource first, which per spec fully
resets element state (aborts outstanding fetches, clears buffered
ranges, resets `readyState` to `HAVE_NOTHING`) before the new
resource is ever assigned — a materially more thorough reset than a
direct `src` reassignment on top of a merely-paused element.

**Verified for real**: ran a live local server against a disposable
DB copy; scripted a rapid A→B→A→B track-switch sequence plus the
original Fix A toggle-race sequence (8 rounds) in real headless
Chromium (script + saved result at
`/private/tmp/claude-501/-Users-srikant-Projects-Aarva/aac3d432-0e19-483c-a805-6dde8192d06f/scratchpad/audio_bug_diag3/verify_fix_e.py`
and `.../audio_bug_diag3/verify_fix_e_result.json`) — zero console
errors, exactly one `<audio>` element throughout, final state
(`readyState=4`, `paused=false`, correct `src`) consistent with the
last click. All 40 existing repo tests still pass (re-run this
session, confirmed).

**What this does NOT verify**: whether the native-buffer-leak
mechanism is the correct explanation, or whether this fix actually
eliminates the leak on real iOS Safari hardware — headless Chromium
uses a different media backend and cannot exercise this iOS-Safari-
specific buffering behavior at all. This is the same category of gap
already flagged for Fix B/C. Ask the user to specifically retry rapid
track-switching (not just the original toggle-race or back-navigation
repros) once this redeploys.

---

## Fix F — scope htmx's history-cache to `#main-content` via `hx-history-elt` (added 2026-07-29, same day, after Fixes A-E were found insufficient a third time)

**How this was found**: the user reproduced overlap a third time
(screen recording `ScreenRecording_07-29-2026 16-30-43_1.mov`, checked
via `ffprobe` in-session — duration 39.1s, creation_time
`2026-07-29T15:40:47Z`, after PR #131/Fix E's merge at `15:26:31Z` —
and confirmed against a fresh close-and-reopen per the user, not stale
cached JS. The video file itself is no longer present on disk as of
this writing — the user's own Downloads-folder cleanup, consistent
with the same file also being absent after the Fix E video — so this
specific ffprobe reading can't be independently re-verified after the
fact; noting that rather than re-asserting it as freshly checked) and
specified the trigger precisely: **"the error initiates once the back button is
pressed, and then if i interchange between the main play/pause button
... and the play/pause button on the player bar."** Independent
spectrogram analysis (regenerated this session at
`/private/tmp/claude-501/-Users-srikant-Projects-Aarva/aac3d432-0e19-483c-a805-6dde8192d06f/scratchpad/audio_bug_diag5/spec_20_28.png`
and `.../spec_29_37.png`) confirmed genuine sustained overlap this
time — a clean silence gap at each track switch (~t=22s, ~t=31.5s,
matching the user's flagged 24s/33s marks) followed by 5+ seconds of
dense, continuous spectral energy with none of the normal word-to-
word pauses, unlike the second video (where the same check had
correctly found no overlap signature).

**The back-button precondition was the key new fact.** It pointed
back at htmx's own history-cache mechanism, which this doc had
already partially investigated (see the original "Agree or disagree
with Cowork's bfcache hypothesis" section above) — but one detail was
never checked: WHICH element `restoreHistory()`'s cache-hit path
actually swaps.

**Root cause, verified against the actual pinned htmx 2.0.10 source**
(`/private/tmp/claude-501/-Users-srikant-Projects-Aarva/aac3d432-0e19-483c-a805-6dde8192d06f/scratchpad/htmx_2.0.10.js`,
matching the SRI-pinned CDN version at `base.html:201`):

1. `getHistoryElement()` (`htmx_2.0.10.js:3148-3151`):
   ```js
   function getHistoryElement() {
     const historyElt = getDocument().querySelector('[hx-history-elt],[data-hx-history-elt]')
     return historyElt || getDocument().body
   }
   ```
   Grepped every template in `aarva/server/templates/`: **no page sets
   `hx-history-elt` or `data-hx-history-elt` anywhere.** So this always
   falls back to `document.body`.

2. Both `saveCurrentPageToHistory()` (`htmx_2.0.10.js:3250`) and
   `restoreHistory()`'s cache-hit path (previously cited in this doc,
   `htmx_2.0.10.js:3341-3358`) call `getHistoryElement()` — meaning
   **every history-cache snapshot AND every history-cache restore
   operates on the whole `<body>`**, not `#main-content`. This is a
   separate code path from normal AJAX-boosted navigation, which
   correctly scopes to `#main-content` via the `hx-target`/`hx-select`
   attributes on `<body>` (`base.html:219-220`) — those attributes
   have no effect on the history-cache path at all.

3. `swap()`'s cache-hit call (`swap(details.historyElt, cached.content,
   ...)`, already quoted in this doc's earlier section) therefore
   replaces the ENTIRE contents of `<body>` on every back/forward
   navigation that hits htmx's cache — including the persistent-
   player `<script>` (`base.html:578-1018` at the time this was
   written, all inside `<body>` but OUTSIDE `<main id="main-content">`,
   which spans `base.html:338-...`), the shared `<audio>` element, and
   the mini-player markup.

4. **The mechanism that turns this into a live bug**: htmx's
   `normalizeScriptTags()` (`htmx_2.0.10.js:577-591`) explicitly
   duplicates every `<script>` tag found in ANY swapped fragment via
   `duplicateScript()` (`htmx_2.0.10.js:549-559`,
   `getDocument().createElement('script')` + copying attributes/text)
   specifically to force re-execution — the function's own comment:
   *"we have to make new copies of script tags that we are going to
   insert because SOME browsers ... don't execute scripts created in
   `<template>` tags."* This is NOT a bug in htmx; it's intentional,
   documented behavior. But it means every body-wide history-cache
   restore **re-runs the entire persistent-player IIFE as a brand-new,
   independent script execution** — with its own `audio`/`current`/
   `pendingPlay` closures, its own newly-created `<audio>` element,
   and (critically) its own "restore state on page load" block
   (`base.html:993-1017`) that reads `sessionStorage` and
   calls `audio.play()` on this NEW element, auto-resuming playback —
   while whatever the PREVIOUS script instance's `<audio>` element and
   listeners are doing is not necessarily stopped.

**Why this explains every symptom in this investigation, not just
this video**: the original bug's smoking-gun evidence (mini-player
and an in-page card showing two different `currentTime` values in
what should be one synchronous `updateUI()` call) requires two
independent script contexts — exactly what this mechanism produces.
The "dissonance" the user asked about separately (mini-bar and
in-page controls not affecting each other) is explained by different
generations' listeners attaching to whichever DOM nodes happen to be
live at the time. And this video's overlap is explained by a freshly
re-executed generation auto-resuming playback via its own `<audio>`
element while an earlier generation's element/listeners may still be
live. Fixes A-E are all still correct and worth keeping (the toggle
race, the pause-before-hide, the pageshow resync, and the full-unload
on track switch are all real, independent robustness improvements) —
none of them could have addressed this, since this operates one level
below all of them: it creates entirely new script instances, each of
which independently has the (now-fixed) A-E behaviors.

**Change**: add `hx-history-elt` to `base.html:338`
(`<main id="main-content">`):
```html
<main id="main-content" hx-history-elt class="max-w-3xl mx-auto px-5 py-10">
```
This is a presence-only attribute (`getHistoryElement()`'s selector
doesn't check its value) — no value needed. With this in place,
`getHistoryElement()` returns `#main-content` for both save and
restore, so history-cache operations never touch anything outside it.
The persistent player's `<script>`, `<audio>` element, and mini-player
markup all live outside `#main-content` and are now permanently out
of scope for this mechanism, matching what the architecture already
assumed for every OTHER navigation path.

**Verified for real, deterministically — no real-device gap this
time**: this is the first fix in the whole investigation whose root
mechanism is pure htmx/DOM behavior rather than an iOS-Safari-only
quirk, so it could be fully verified in headless Chromium:

- Ran a live local server against a disposable DB copy. Stashed a
  JS-side reference to the `<audio>` element and mini-player before a
  back-navigation, then compared object identity after — run once
  with the fix temporarily reverted (script + result saved at
  `/private/tmp/claude-501/-Users-srikant-Projects-Aarva/aac3d432-0e19-483c-a805-6dde8192d06f/scratchpad/backnav_check/check_backnav_before_fix_f.py`
  and `.../backnav_result_before_fix_f.json`) and again with the fix
  applied
  (`.../backnav_check/check_backnav_after_fix_f.py` and
  `.../backnav_result_after_fix_f.json`). **Before this fix**:
  `audio_is_same_node: false`, `mini_player_is_same_node: false` —
  confirming the swap really did replace both with brand-new nodes,
  and the new audio element was already playing on its own
  (`new_audio_paused: false`) despite no script in the test ever
  calling play — direct proof of the spurious re-execution
  auto-resuming playback. **After this fix**:
  `audio_is_same_node: true`, `mini_player_is_same_node: true`,
  `old_audio_still_in_document: true`, and `currentTime` advancing
  continuously across the navigation (`1.58s` → `3.09s` over the same
  wait) — the same element, uninterrupted, no second instance.
- Full regression pass (script + result at `.../backnav_check/full_regression.py`
  and `.../full_regression_result.json`): normal htmx-boosted
  navigation still correctly changes page content; the same audio
  node and continuous playback survive **3 consecutive** back/forward
  cycles; the Fix A toggle-race sequence (8 rounds) still shows
  exactly one `<audio>` element with zero errors; the Fix E track-
  switch sequence still works correctly. All 40 existing repo tests
  still pass (re-run this session).

**What this does NOT change**: Fixes A-E's code is untouched and
still shipped. This fix specifically closes the "back button, then
interchange between the two controls" path; if a future report
doesn't involve back/forward navigation at all, it's a different
mechanism and this fix wouldn't be expected to touch it.

---

## Not fixing (out of scope)

- The exact browser-internal mechanism behind Fix B/C's target
  scenario (true bfcache freeze vs. a WKWebView-specific tab-suspend)
  remains unconfirmed — per the diagnosis doc, pinning that down
  further would need on-device Web Inspector access beyond what's
  available in this environment. Fix B is designed to be correct
  regardless of which exact mechanism is at play (proactively pausing
  before backgrounding closes the gap either way).
- Not adding a cross-tab `BroadcastChannel`/`localStorage`-event
  reconciliation layer. `sessionStorage` is already per-tab; nothing
  in the current bug reports suggests multi-tab simultaneous
  listening is in scope.

---

## Files that change

- `aarva/server/templates/base.html` only — Fixes A-D all live
  inside the existing persistent-player `<script>` block
  (`base.html:578-970`). No new files, no schema, no route changes.
- `docs/roadmap.md` — move this item to "Recently completed" once
  shipped, per AGENTS.md rule 17a (Claude Code's own responsibility
  here since Claude Code is authoring and shipping this spec).

---

## Verification plan

- **Headless Chromium (Playwright), the toggle race (Fix A)**: drive
  a real local server against a disposable DB copy with a real
  playable article; script rapid alternating clicks between the
  in-page play button and the mini-player toggle button; confirm no
  unhandled promise rejection reaches the console and that `audio.
  paused` settles to a value consistent with the last click once all
  pending promises resolve (not left in an indeterminate state).
- **Regression check**: confirm a single tap on either button still
  toggles play/pause correctly and immediately (no added perceptible
  delay for the common, non-racing case).
- **Fix D regression check**: confirm switching between two different
  tracks (clicking a different article's play button while one is
  already playing) still starts the new track correctly from 0:00.
- **Fix B/C**: `pagehide`/`pageshow` with `event.persisted` can't be
  fully driven in headless Chromium the same way real iOS Safari
  bfcache behaves. Confirm the listeners are wired (present in the
  rendered page, no console errors on load) and, where Playwright's
  navigation APIs allow, exercise a same-origin back/forward
  navigation to confirm `wireDataPlayers()`/`updateUI()` re-run
  without error. Flagging real-device confirmation as the strongest
  signal for this specific path, same category of gap
  `docs/session_plan_ios_player_bugs.md` (2026-07-18) already
  documented for iOS-Safari-specific behavior — ask the user to
  retry the ORIGINAL back-navigation-based repro (not just the
  toggle-tap repro, which Fix A already targets directly) once
  shipped.
