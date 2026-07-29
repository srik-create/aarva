**STATUS: Code shipped 2026-07-29. Real-device confirmation of Fix
B/C (the bfcache/backgrounding path) PENDING** — headless Chromium
can't reproduce iOS Safari's bfcache lifecycle, so that path is
verified by code review + regression tests only. Fix A (the toggle
race) was verified directly: 12 rounds of rapid alternating taps in
a real headless-Chromium run produced no errors, no duplicate audio
elements, and correct final state. Ask the user to retry the
ORIGINAL back-navigation repro (not just toggling) once this is live.

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
(`base.html:809-811`: `audio.pause(); audio.removeAttribute('src');
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
