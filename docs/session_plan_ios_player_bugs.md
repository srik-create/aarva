# Session plan — iOS player bugs (lock-screen metadata + fixed-position scroll)

Written by Cowork for the next Claude Code session (2026-07-18+).
Two independent iOS-only bugs in the shared audio player caught
on 2026-07-18 during real listening. Small self-contained fixes
in `aarva/server/templates/base.html`. Ship as one PR.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

**Mockup gate**: iOS-Safari-specific behaviour is hard to
reproduce in a headless browser. Claude Code MUST verify the
fixes on the actual live site from an iPhone (via the user's
device) before landing the PR — a desktop-Chrome check is not
sufficient here. Both fixes touch listener-facing surfaces, so
AGENTS.md rule 4 (listener-facing copy is exempt, but the
overall UX gate applies) — walk the user through the check.

---

## Context

Two iPhone-only regressions surfaced 2026-07-18:

### Bug 1 — Lock Screen / Control Center shows stale track name

Symptom: iPhone lock screen and Control Center's Now Playing
widget display the wrong episode title while a different track
is actually playing in the web app. Screenshots on the day:
lock screen shows "Aarva — 2026-07-17" (yesterday's daily
edition) while the browser is playing the crosscut "Using Deep
Research Versus Party Loyalty". Control Center at a different
moment showed "A Gujarati Dalit w…" (an article title from an
earlier session) while the same crosscut was playing.

Root cause: `aarva/server/templates/base.html`'s shared player
never calls `navigator.mediaSession.setMetadata()`. iOS's Now
Playing widget reads from the Media Session API; without an
app-supplied `MediaMetadata`, iOS infers the title from the
FIRST audio surface it observed (usually the document `<title>`
at initial `audio.play()`) and never picks up subsequent
updates when the shared audio's `src` is swapped for a new
track (line 636 of `base.html`: `audio.src = src` in
`playTrack()`).

`grep -rnE "mediaSession|MediaMetadata" aarva/server/` returns
zero hits — the API isn't wired at all.

### Bug 2 — Mini-player bar lags mid-scroll during momentum scrolling

Symptom: while scrolling a long page on iPhone Safari, the
persistent mini-player bar temporarily sits in the MIDDLE of
the viewport instead of staying glued to `bottom: 0`. Once
scroll momentum settles, the bar snaps back to the bottom.
Video recording on 2026-07-17 captured the anomaly clearly at
t=3.0s — the bar appears between "Every day, a handpicked
selection of…" and "topics. Gathered, narrated…" with content
above AND below it.

Root cause: iOS Safari's long-standing `position: fixed` +
momentum-scroll bug. During elastic/rubber-band scrolling,
fixed elements can render as if they were `position: absolute`
until momentum settles. The mini-player at `base.html` line 461
is correctly declared `fixed bottom-0 inset-x-0 z-40` — no
ancestor has a `transform` / `filter` / `will-change` that
would trap it in a containing block. This is a rendering
quirk, not a CSS mistake.

---

## Fix 1 — Wire the Media Session API in the shared player

### Changes in `aarva/server/templates/base.html`

Inside `playTrack()` (currently line 628+), after `audio.src = src`
but before `audio.play()`, add:

```js
if ('mediaSession' in navigator) {
  navigator.mediaSession.metadata = new MediaMetadata({
    title:  current.title || 'Aarva',
    artist: 'Aarva',
    album:  'Aarva',
    artwork: [
      { src: '/static/icons/apple-touch-icon.png',
        sizes: '180x180', type: 'image/png' },
      { src: '/static/icons/icon-512.png',
        sizes: '512x512', type: 'image/png' },
    ],
  });
}
```

Register action handlers ONCE (module init, not inside
playTrack) so lock-screen / CC / connected-Bluetooth controls
stay wired to the shared audio element across src swaps:

```js
if ('mediaSession' in navigator) {
  navigator.mediaSession.setActionHandler('play',  function() {
    if (audio.src) audio.play().catch(function(){});
  });
  navigator.mediaSession.setActionHandler('pause', function() {
    audio.pause();
  });
  navigator.mediaSession.setActionHandler('seekbackward', function(details) {
    audio.currentTime = Math.max(0, audio.currentTime - (details.seekOffset || 10));
  });
  navigator.mediaSession.setActionHandler('seekforward', function(details) {
    audio.currentTime = Math.min(audio.duration || 0,
                                 audio.currentTime + (details.seekOffset || 10));
  });
  // seekto handler for lock-screen scrubber (iOS 15+):
  navigator.mediaSession.setActionHandler('seekto', function(details) {
    if (details.fastSeek && 'fastSeek' in audio) {
      audio.fastSeek(details.seekTime);
    } else {
      audio.currentTime = details.seekTime;
    }
  });
}
```

Also mirror `audio.currentTime` / `audio.duration` into
`navigator.mediaSession.setPositionState(...)` on `timeupdate`
(throttled, alongside the existing `saveState` throttle at
line 649-659), so the lock-screen scrubber shows real progress:

```js
if ('mediaSession' in navigator && audio.duration) {
  navigator.mediaSession.setPositionState({
    duration:     audio.duration,
    playbackRate: audio.playbackRate,
    position:     audio.currentTime,
  });
}
```

Wrap `setPositionState` in try/catch — some browsers throw if
`duration` is NaN or position exceeds duration by a rounding
epsilon.

### Artwork asset check

The spec uses `/static/icons/apple-touch-icon.png` (already
present per `base.html` line 137) and `/static/icons/icon-512.png`
(check if present — if not, use only apple-touch-icon in the
artwork array; iOS accepts a single-element list). Don't ship
a new asset in this PR unless one is trivially derivable from
the existing icons.

### Verification (device-side)

1. Play a track from `/today` (a daily-edition article).
2. Lock the iPhone. Confirm the lock-screen Now Playing widget
   shows THAT article's title, not "Aarva — <date>" or an older
   title.
3. Unlock, navigate to `/crosscut/<id>` for a different piece,
   tap play. Confirm the lock-screen title UPDATES to the new
   crosscut title within a second.
4. From the lock screen, tap play/pause and use the scrubber.
   Confirm the web app's audio responds and progress reflects
   accurately in both surfaces.
5. Open Control Center while a track is playing. Confirm the
   Now Playing tile shows the current track (not a stale one).

---

## Fix 2 — Promote the mini-player to its own GPU compositor layer

### Change in `aarva/server/templates/base.html`

Add to the `<style>` block near the existing mini-player CSS
(line 96-110):

```css
/* iOS Safari momentum-scroll workaround: position:fixed
   elements can briefly render as position:absolute during
   elastic overscroll, letting the bar lag mid-viewport until
   scroll settles. Promoting the bar to its own compositor
   layer eliminates the lag with essentially zero cost. */
[data-mini-player] {
  transform: translateZ(0);
  -webkit-backface-visibility: hidden;
  backface-visibility: hidden;
  will-change: transform;
}
```

### Watch-out: containing-block interaction

`transform` on an element establishes a new containing block
for its `position: fixed` descendants. `[data-mini-player]`
itself is fixed, not its parent — so this is safe. But the
speed menu (`[data-mini-speed-menu]`, line 492) inside the bar
uses `absolute` positioning, not `fixed`, so it also stays
unaffected.

If any future change adds a `position: fixed` element INSIDE
`[data-mini-player]` (unlikely), that element would be trapped
by the new containing block. Add a comment in the CSS so the
next reader knows.

### Verification (device-side)

1. Load `/today` on iPhone Safari with an audio track playing
   (mini-player visible).
2. Scroll the page vigorously — flick to trigger momentum
   scrolling and elastic overscroll at both ends.
3. Confirm the mini-player STAYS at `bottom: 0` throughout the
   momentum animation. It should never appear mid-viewport
   with content below it.
4. Repeat on a long article page (`/article/<id>`) and a
   crosscut page (`/crosscut/<id>`) — both are common surfaces
   where the bar would previously float mid-scroll.

---

## Non-goals

- Don't add per-article artwork (would require Stage-9 or
  Stage-10 art generation). Site-wide Aarva mark is fine.
- Don't change the mini-player's layout, size, or interaction
  model. Fix 2 is a single CSS block — no HTML changes.
- Don't refactor `#main-content` into its own scroll container
  (a different iOS-scroll-quirk mitigation, but far more
  invasive; not needed if Fix 2 works).

---

## Files that change

- `aarva/server/templates/base.html` — Media Session wiring
  inside the existing `<script>` block (Fix 1), and a small CSS
  addition in the `<style>` block (Fix 2).
- `docs/roadmap.md` — after this PR merges, move the item from
  In-Progress to Recently Completed (Claude Code owns this per
  AGENTS.md rule 17).
