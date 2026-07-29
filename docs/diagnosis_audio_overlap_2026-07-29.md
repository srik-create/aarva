**Diagnosis only — no spec, no code, no PR. Written 2026-07-29 in
response to Cowork's bug report + leading bfcache hypothesis.**

**Superseded by `docs/session_plan_audio_player_overlap_fix.md`
(2026-07-29, same day)** — the user asked Claude Code to write the
fix spec directly rather than hand this diagnosis to Cowork. This
doc remains the historical record of the investigation; the fix
design and shipped code live in the session_plan doc.

---

## TL;DR

**Agree with the direction of Cowork's hypothesis, disagree with the
specific mechanism.** Two independent audio-driving contexts (two
separate executions of the persistent-player IIFE in
`aarva/server/templates/base.html:578-970`, each with its own
`audio` reference and its own `current.src`/`updateUI()` cycle) are
definitely coexisting — this is provable from the evidence alone,
independent of any theory about *how* the second context got
created. The most likely trigger is still page-lifecycle-related
(native browser back/forward navigation — via the in-app back arrow
**or**, just as plausibly, iOS Safari's edge-swipe-back gesture,
which bypasses the in-app link entirely), but I disproved one part
of Cowork's proposed mechanism by reading htmx 2.0.10's actual
source: **`htmx:afterSwap` does fire during htmx's own history-cache
restoration** (confirmed at the bytecode level below), so this is
not simply "wireDataPlayers() never re-runs after back navigation."
The sustained (not momentary) nature of the overlap is fully
explained by a separate, simpler fact: **nothing in `base.html`
ever detects or cleans up a second audio-driving context if one
exists** — the whole architecture assumes exactly one, and there is
no `pageshow` handler, no periodic reconciliation, and no code path
that ever calls `document.querySelectorAll('audio')` (only
`getElementById` for the one it expects).

---

## Root cause — ranked

### #1 (primary): a second execution of the persistent-player script exists, most likely triggered by native back/forward navigation interacting with iOS Safari's page-lifecycle behavior (bfcache or equivalent), and `base.html` has no mechanism to detect or reconcile a second context if one appears

**Evidence, independently re-derived, not just taken from Cowork's summary:**

I re-extracted both pieces of evidence myself from the source `.mov` files (paths in Cowork's report) using `ffmpeg`/`ffprobe` rather than trusting the description alone.

- **Spectrogram (video 1, `showspectrumpic`)**: confirmed independently — the first ~17s show clear silent gaps between phrases (single voice); from ~17s onward, spectral density increases sharply with almost no gaps, consistent with two voices overlapping. This confirms the overlap is real audio, not a UI glitch. (Screenshot generated at `/private/tmp/.../scratchpad/audio_bug_diag/v1_spectrogram.png` during this session.)

- **Video 2, frame-by-frame (own extraction, 1fps)**: I pulled 21 frames and traced the actual sequence, not just the single "smoking gun" frame Cowork quoted:
  - Frames 1-12 (~0-12s): user on the article-detail page for "Opinion: Ebola Outbreak Response…" (`STAT News`). Mini-player ticks smoothly and continuously: 0:33 → 0:36 → 0:36 (a pause/resume, matching a visible play↔pause icon toggle) → 0:40 → 0:41. Fully consistent, single continuous audio stream, 1.25× speed shown throughout.
  - Between frame 12 (article-detail, mini-player 0:41) and frame 13, the view changes to `/today` (JTBD-grouped list — this is the back-arrow's documented fallback target for any non-`/today` page: `aarva/server/templates/base.html:344`, `{% set back_fallback = '/' if request.url.path == '/today' else '/today' %}`). This is circumstantial but strong: the article-detail page is the *only* page in this sequence with a visible back arrow, and `/today` is exactly where that arrow points.
  - **Frame 13, the actual smoking gun (re-confirmed myself, slightly different exact numbers than Cowork's quote but the same phenomenon)**: on `/today`, the Ebola card shows **PAUSE, 1:00 / 6:51**; the mini-player at the bottom of the *same screenshot* shows **PAUSE, 0:40 / 6:51**. All *other* cards on this same `/today` render (the crosscut card, the Asterisk "Future-gazing" card) correctly show their untouched default `0:00` — confirming this is a fresh-looking `#main-content` render, not visual corruption, and that only the currently-"playing" card's number is anomalous.

**Why this proves two independent contexts, not a single-audio timing glitch:** `updateUI()` (`base.html:684-729`) sets the mini-player's time (line 687: `miniCurrent.textContent = fmt(audio.currentTime)`) and any matching in-page card's time (line 719-721, gated on `trackSrc === current.src`) from the **same `audio.currentTime` read, in the same synchronous function call**. There is no code path by which one call to `updateUI()` produces two different numbers for the same underlying value. For the mini-player and an in-page card showing the same track to disagree by ~20 seconds, there must be two different `audio` element / `current.src` / `updateUI()` triples driving them independently — i.e., two separate executions of the IIFE at `base.html:578-970`, each believing it owns "the" shared player.

**Why the overlap is sustained, not a one-frame glitch:** nothing in `base.html` ever looks for a second audio-driving context. The controller reads `document.getElementById('aarva-shared-audio')` once per execution (line 584) and never re-checks. There's no `pageshow` listener (only `beforeunload` and `pagehide`, lines 942-943) to detect "this document was just reactivated, reconcile against reality." There's no periodic `document.querySelectorAll('audio')` sanity check anywhere. Once a second context exists, both just keep running independently until whichever document/tab they belong to is actually destroyed — which, per the video evidence (video 1 is 34s long with dense overlap for the back ~17s of it), can be tens of seconds at minimum.

**What I could not pin down precisely, and why:** I cannot state with certainty *which exact browser-level event* creates the second document/context (true bfcache page-freeze-and-thaw vs. a WKWebView tab-suspend-and-reload vs. something else iOS-Safari-specific) without a live, on-device repro with Safari Web Inspector attached. This requires physical access to the iPhone in question, which I don't have in this environment. I've flagged the single highest-value next diagnostic step below.

### #2 (contributing, not sufficient alone): `playTrack()` never pauses the outgoing track before swapping `src`

`base.html:739-741`:
```js
current = { src: src, title: title || '', link: link || '/today' };
audio.src = src;
audio.currentTime = 0;
```
No `audio.pause()` first, unlike the close-button handler (`base.html:808-811`: `audio.pause(); audio.removeAttribute('src'); audio.load();`), which does it correctly. Per the HTMLMediaElement spec, assigning a new `src` does invoke the "media element load algorithm," which aborts the in-flight resource for the *old* `src` — but on a **single** audio element this produces, at most, a brief (sub-second) artifact during the codec/buffer transition, not a sustained 17-second dual-voice overlap. **This is a real robustness bug worth fixing regardless, but it cannot by itself explain the evidence** — it doesn't create a second element, and it doesn't explain why the overlap in video 1 sustains for ~17 of 34 seconds.

### #3 (unlikely primary driver, same reasoning as #2): the pending-`play()`-promise race at line 761

Same conclusion as #2: on a single `<audio>` element, iOS Safari's media-load algorithm aborts the previous fetch/decode when `src` is reassigned; this is a possible source of a very brief artifact, not a sustained one. Ranked below #2 because it requires a more specific timing window to manifest at all.

---

## Agree or disagree with Cowork's bfcache hypothesis?

**Partially agree, with one part of the proposed mechanism directly disproved.**

**Agree:** the general shape — some page-lifecycle event around the native-history back-navigation link (`base.html:345-352`, `hx-boost="false"` + `history.back()`) results in a second, independent audio-driving context existing alongside the current one — matches the evidence better than any alternative I could construct. I could not find any other code path (duplicate-listener registration, MediaSession quirks, the `src`-swap race) that produces *two different `audio.currentTime` values read in the same `updateUI()` call* — that specific symptom requires two separate contexts, and the back-navigation link is the one deliberate escape hatch from htmx's single-document SPA-style navigation in this whole codebase.

**Disagree with this specific claim in Cowork's writeup:** *"iOS Safari's back-forward-cache (bfcache) × the hx-boost="false" back button... [creates] potentially TWO `<audio>` elements... because native back navigation on iOS Safari uses bfcache, which restores a complete page snapshot."* This framing implies the fix should focus on `hx-boost="false"` / htmx's history-cache handling specifically colliding with bfcache. I read htmx 2.0.10's actual bundled source (fetched the exact pinned CDN version, `https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.js`, matching `base.html:201`'s SRI-pinned version) to check this precisely, since it's directly falsifiable:

```js
// htmx.js:3341-3358 (restoreHistory)
function restoreHistory(path) {
  saveCurrentPageToHistory()
  path = path || location.pathname + location.search
  const cached = getCachedHistory(path)
  if (cached) {
    const swapSpec = { swapStyle: 'innerHTML', swapDelay: 0, settleDelay: 0, scroll: cached.scroll }
    const details = { path, item: cached, historyElt: getHistoryElement(), swapSpec }
    if (triggerEvent(getDocument().body, 'htmx:historyCacheHit', details)) {
      swap(details.historyElt, cached.content, swapSpec, { contextElement: details.historyElt, title: cached.title })
      ...
      triggerEvent(getDocument().body, 'htmx:historyRestore', details)
    }
  } else { ... }
}
```

`restoreHistory()` on a cache hit calls the **same internal `swap()`** used for every normal AJAX-driven navigation, and `swap()` (`htmx.js:1979-1982`) unconditionally does:
```js
forEach(settleInfo.elts, function(elt) {
  ...
  triggerEvent(elt, 'htmx:afterSwap', swapOptions.eventInfo)
})
```
So **`htmx:afterSwap` does fire** on a history-cache restore (the event bubbles to `document.body`, where `base.html:935` listens), meaning `wireDataPlayers()` and `updateUI()` *do* get invoked after a back-navigation that hits htmx's own cache. htmx also confirmed (per its docs, fetched during this session) that what it caches is the **live, post-mutation DOM**, not pristine server HTML — so a restored card *can* legitimately show a stale-but-real number that was baked in at snapshot time — but that's a display-staleness nuance, not the audio-duplication mechanism Cowork described. **htmx's own history cache is not, on the evidence I could gather, the thing creating a second `<audio>` element.** The more likely culprit is genuinely one level below htmx — the browser's own page lifecycle (bfcache or a WKWebView-specific equivalent) creating (or reviving) an entire second document, each with its own copy of `base.html`'s script, independent of anything htmx does.

**One correction to Cowork's framing of the trigger itself:** the report frames the back-arrow *link* (`hx-boost="false"`) as the trigger. But `history.back()` is a native browser API — it responds to **any** back-navigation, including iOS Safari's system-level edge-swipe-back gesture, which works on every page regardless of whether the site has its own back button, and bypasses the in-app `<a onclick>` entirely. If the real mechanism is bfcache/page-lifecycle-based, the edge-swipe gesture is at least as likely a trigger as the in-app arrow, and probably *more* likely given how natural it is during normal one-handed iPhone browsing (matches the bug report's framing: "browsing aarva.app normally"). This matters for scoping any eventual fix — a fix that only guards the custom back-arrow's `onclick` handler would not close the edge-swipe path, since that never runs any of Aarva's JS before triggering `popstate`.

---

## Verification performed

- **Independently regenerated the spectrogram** from the source video (not just trusted Cowork's description) — confirmed the same silence-then-density pattern.
- **Independently extracted 21 frames (1fps) from video 2** and traced the full sequence, not just the single frame Cowork quoted — confirmed continuous, consistent playback for 12 seconds on the article-detail page, then an instant, unexplained divergence the moment `/today` appears, with all *other* cards on that page showing correct, untouched defaults (ruling out generalized rendering corruption — only the "currently playing" card is affected).
- **Read the entirety of `aarva/server/templates/base.html`'s player script** (lines 312-970 plus the surrounding markup) line by line, not just grepped for symbols.
- **Fetched and read the actual pinned htmx 2.0.10 source** (`https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.js`, matching the exact SRI hash in `base.html:201`) to verify, rather than assume, whether `htmx:afterSwap` fires during history-cache restoration. Confirmed it does (see code excerpt above) — this **disproves** the "afterSwap never fires on back-nav, so wireDataPlayers/updateUI silently stop running" theory I initially suspected while reading `base.html` alone.
- **Fetched htmx's own documentation** on history-cache behavior to confirm it snapshots live (post-mutation) DOM, not static server HTML — relevant to why a restored card can show a stale-but-plausible number.
- **Confirmed `hx-swap="innerHTML"`** (`base.html:220`) with no idiomorph/morph extension in use, and read `swapInnerHTML`'s implementation (`htmx.js:1779-1790`) — confirmed old DOM nodes are genuinely detached and cleaned up (`cleanUpElement` + `removeChild`) on every swap, network or cache-restored. This confirms Cowork's dismissal of the `wireDataPlayers()` duplicate-listener risk is **correct** — old listeners die with the old (removed) nodes on every swap style used here.
- **Did not/could not do**: run `document.querySelectorAll('audio').length` live during an actual reproduction. I have no access to the reporting user's iPhone or a live remote-debugging session from this environment. This is the single most decisive missing data point — see recommendation below.

---

## What Cowork's grep-based analysis missed

1. **It never checked whether `htmx:afterSwap` actually fires during history-cache restoration** — it noted `wireDataPlayers()` lacks an idempotency guard, reasoned (correctly, per my independent check) that this doesn't matter for *normal* swaps because old nodes are discarded, but didn't go one level deeper to ask "does the history-cache-restore code path even go through the same `swap()`/`afterSwap` machinery, or a different one?" It does — I had to read htmx's actual bundled source to confirm this, which is a level of verification beyond grepping Aarva's own code.
2. **It treated `hx-boost="false"` + `history.back()` as the whole story**, without considering that native back/forward navigation is *also* reachable via iOS Safari's edge-swipe gesture, which doesn't touch this specific link's `onclick` at all. Any fix scoped only to the in-app arrow would miss this.
3. **It didn't independently re-derive the frame-by-frame sequence from video 2** — it quoted one frame (the clearest smoking gun) but didn't trace the ~12 seconds of *consistent, correct* playback immediately preceding it. That preceding sequence is actually important evidence: it rules out "the bug happens gradually/randomly during normal playback" and pins the divergence to the *exact moment* of the page transition to `/today`, which is what makes the back-navigation theory well-supported rather than speculative.
4. **It didn't flag the missing `pageshow` handler** as the specific, nameable gap that explains why the bug is *sustained* rather than self-correcting. Rules 17d/17e-style "cite the specific line" discipline is worth applying here too: the absence of a `pageshow` listener (verifiable by grep — `grep -n pageshow base.html` returns nothing) is a concrete, citable fact that directly explains the "sustained not momentary" part of the symptom, independent of which exact page-lifecycle event triggers the duplication in the first place.

---

## Recommended next diagnostic step (not a fix — still diagnosis)

The single highest-value thing that would convert "high-confidence hypothesis" into "confirmed root cause": during a live reproduction on the actual iPhone, with Safari's Web Inspector attached (Mac + iPhone via USB/Safari > Develop > [device] > aarva.app), run in the console at the moment the dual-playhead symptom appears:

```js
document.querySelectorAll('audio').length
```

If it returns `1`: the two audio-driving contexts are NOT both live DOM elements of the *same* document — meaning the second context belongs to a genuinely separate document (bfcache/tab-suspend scenario), which the current document has no way to observe or influence, and the fix has to be page-lifecycle-based (a `pageshow` handler forcing a clean resync, and/or explicitly pausing on `pagehide`/visibility-change rather than just saving state).

If it returns `2` (or more): there are two `<audio id="aarva-shared-audio">` elements in the *same* live document — meaning the entire `base.html` markup (not just `#main-content`) got inserted a second time somehow, which would be a more surprising and more directly fixable finding (something is duplicating the whole page shell, not just failing to clean up between documents).

Also worth checking in the same session: `performance.getEntriesByType('navigation')[0].type` — if it reads `"back_forward"` at the moment of the bug, that's direct confirmation the browser itself is treating this as a bfcache-eligible navigation, versus `"navigate"` which would point elsewhere entirely.

I was not able to run this myself — no device or remote-debugging access from this environment. This is exactly the kind of on-device-only verification `docs/session_plan_ios_player_bugs.md` (2026-07-18, a related past player bug) already flagged as a category of check that "can only be confirmed on real iPhone hardware."

---

## UPDATE 2026-07-29 (same day) — live on-device check result + a new repro trigger, second root cause identified

The user ran the recommended check live on their iPhone via Safari Web Inspector during a real reproduction. Results:

- `document.querySelectorAll('audio').length` → **`1`**, confirmed twice.
- `performance.getEntriesByType('navigation')[0].type` → **`"navigate"`**.
- New information from the user, not previously reported: **the overlap "seems to be triggered very much" by rapidly toggling play/pause by alternating taps between the in-page article play button and the mini-player bar's toggle button.**

**Correction to my own immediately-preceding verbal summary to the user:** I initially told the user this result "directly refutes" the two-context theory above. That was wrong, and inconsistent with what this very doc already said before the check was run (see the "Recommended next diagnostic step" section, the paragraph starting "If it returns `1`") — an `audio.length === 1` result is fully **consistent with** the two-document/bfcache theory, not a refutation of it. Safari Web Inspector's console only ever inspects the single frontmost, active document. If a second audio-driving context exists because a *different* Document object is frozen in bfcache (not currently on-screen), that document's `<audio>` element is invisible to a query run against the active document — there is no way to query into a bfcached page's DOM from the visible page's console. So this result rules out only "two `<audio id="aarva-shared-audio">` elements in the one currently-active document" (root cause #1's less-likely sub-variant), not the primary bfcache/second-document hypothesis.

Similarly, `navigation.type === "navigate"` is weaker evidence than it looks: this field reflects how the *current* document was originally loaded and does not get a new value when a document is thawed from bfcache (a bfcache restore is the same Document object, not a new navigation). It only tells us the currently-active page wasn't itself served from a cross-document back/forward at the moment the check ran — it says nothing about whether the check happened to run before or after a relevant navigation, or about a separate frozen sibling document.

**However, the new toggle-only repro trigger changes the ranking.** The user reports the bug reproduces "very much" just from alternating taps between two buttons, with no mention of any back-navigation being involved. This points at a second, independent, and much easier-to-hit root cause on the **same single audio element** — not a duplicate-document issue at all:

### #4 (newly identified, likely primary for this trigger path): uncoordinated, redundant play/pause toggle handlers racing on the one shared `audio` element

Two separate, independently-registered click handlers both gate on `audio.paused` and both call `audio.play()`/`audio.pause()` directly, with zero coordination between them:

- `playTrack()`'s "same track" branch, `base.html:733-737`:
  ```js
  if (current.src === src) {
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

Per the HTML spec, `HTMLMediaElement.play()` sets `.paused` to `false` **synchronously**, before the returned promise resolves and before audible playback actually begins natively. Alternating taps between the two buttons fire these two handlers in quick succession — each one trusts `.paused` as authoritative and immediately issues a new `play()`/`pause()` call against whatever native decode/output pipeline is already mid-transition from the previous call. Both call sites swallow every rejection silently (`.catch(function(){})`), including the well-documented `AbortError: The play() request was interrupted by a call to pause()` that browsers throw specifically when this kind of rapid interruption happens — confirmed as a common, named failure mode of exactly this pattern (rapid alternating play()/pause() calls), not something specific to this codebase. See sources below.

**What I could not confirm via citation, and am flagging as reasoned inference rather than a verified fact:** whether WebKit's native (AVFoundation-backed) teardown of an interrupted `play()` is guaranteed synchronous with the JS-visible `.paused` flag. If it is not — i.e., if a `pause()` call can return before the native pipeline has actually stopped emitting the in-flight decoded buffer — then a subsequent `play()` (from the *other* button, moments later) could start a **new** output stream from the current `audio.currentTime` while the previous, not-yet-fully-torn-down stream is still audibly finishing a buffered chunk from an **earlier** position. That would produce exactly what the user described: "two tracks (or two positions of the same track) simultaneously" — a position-offset overlap of the *same* track, which this mechanism predicts more precisely than the two-document theory does (two independent documents would more likely diverge to noticeably different positions over a longer window, not brief overlapping snippets tied directly to rapid tapping). I searched for a WebKit-specific bug report nailing this exact mechanism and did not find one directly on point — this paragraph is my best mechanistic reasoning from adjacent, confirmed facts, not an independently verified citation. Flagging the gap rather than asserting it as settled.

**Revised ranking given all evidence to date:**

1. **The toggle-handler race (new #4 above)** is now the best-supported explanation for at least one real reproduction path, because the user can trigger it directly and repeatedly through toggling alone, with no navigation involved at all — something root cause #1 (bfcache/second-document) cannot explain on its own.
2. **The bfcache/second-document theory (original #1)** remains unrefuted (see correction above) and still best explains the *original* video evidence, where the divergence coincided exactly with a page transition to `/today` following what looked like back-navigation. It may be a separate, rarer trigger of a similarly-shaped symptom via a different mechanism, or the two could compound (e.g., a toggle-race happening inside a bfcache-frozen document that later re-activates).
3. Root causes #2/#3 (the missing `pause()` before `src` swap; the `play()`-promise race on `src` reassignment) stand as previously ranked — real but insufficient alone to explain a sustained, multi-second overlap.

These are not mutually exclusive — there may be two distinct bugs sharing a similar symptom, both real, both worth fixing.

**Suggested next diagnostic step (still diagnosis, not a fix):** during a live toggle-triggered repro, check `document.querySelectorAll('audio').length` at the moment of overlap (to confirm this trigger path also stays at 1, isolating it from the bfcache path) and, if feasible, temporarily change both `.catch(function(){})` calls to `.catch(function(e){ console.log('play/pause race:', e.name, e.message, audio.currentTime); })` to surface the swallowed `AbortError`s and their timing/position during a real repro — this would directly confirm or rule out the interrupted-promise mechanism rather than relying on inference.

**Sources consulted for the WebKit/interrupted-play() research (web search, 2026-07-29):**
- [How to Prevent "The play() request was interrupted by a call to pause()" Error](https://www.xjavascript.com/blog/how-to-prevent-the-play-request-was-interrupted-by-a-call-to-pause-error/) — confirms this exact error is a documented, common failure mode "especially common in interactive applications where users rapidly trigger play/pause actions," consistent with (but not iOS/WebKit-specific proof of) the mechanism above.
- No WebKit-bug-tracker report was found that directly documents audible overlapping playback (as opposed to a thrown/console error) resulting from this pattern — treat that specific leap as unverified inference, not a cited fact.
