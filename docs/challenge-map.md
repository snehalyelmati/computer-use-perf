# Browser Navigation Challenge — Complete Map

## Overview

The site at `https://serene-frangipane-7fd25b.netlify.app/` is a 30-step challenge.
Each START click randomly assigns a **version** (1–5). The version determines which
challenge type appears at each step. The challenge pool is fixed (22 distinct types),
but the mapping to steps varies.

The site is a **React SPA on Netlify** — all navigation is client-side via React Router.
Full page loads to step URLs return 404 (no server-side routing). Client-side skip via
`pushState` + `popstate` is possible and renders the target step correctly.

---

## Challenge Types (22 types)

| ID | Type | Mechanic | Agent Strategy |
|----|------|----------|----------------|
| 1 | `scroll_reveal` | Scroll down ≥500px to reveal code | `scroll dy=600` |
| 2 | `delayed_reveal` | Code appears after 4s wait | `wait 4500ms`, then read code |
| 3 | `visible_code` | Code shown directly on page | Read from useful_text_lines, submit |
| 4 | `hidden_dom` | Click element 3 times to reveal | Click target element 3x |
| 5 | `click_reveal` | Click "Reveal Code" button | Click Reveal, read code, submit |
| 6 | `memory` | Code flashes 2s, then disappears | Watch text, click "I Remember" for real code |
| 7 | `hover` | Hover box ≥1s to reveal | `hover_element` 1500ms |
| 8 | `drag_and_drop` | Fill 6 slots with pieces | Drag each piece to a slot |
| 9 | `keyboard_sequence` | Press 4 keys in order | `press_key_combination` per key |
| 10 | `audio` | Play audio, then complete | Click "Play Audio", wait, select correct radio, click "Complete Challenge" |
| 11 | `video` | Seek to target frame (42/43/44) | Click "Frame N" button, click "+1"/"-1" to seek, then "Complete Challenge" |
| 12 | `scatter` | Find and click 3-4 scattered parts | Click each part element on the page |
| 13 | `base64` | Base64 hint + "Reveal" button | Ignore the encoded string, click "Reveal" for the real code |
| 14 | `rotating_capture` | Code changes every 3s, click Capture 3x | Click "Capture" 3 times with waits between |
| 15 | `multi_action` | 4 actions: click + hover + type + scroll | Complete all 4, then click "Complete (4/4)" |
| 16 | `math_puzzle` | Solve `N + M = ?` to reveal code | Type answer in number input, click "Solve" |
| 17 | `multi_tab` | Visit 3-5 tabs | Click each Tab button |
| 18 | `canvas_draw` | Draw shape (triangle/square) on canvas | Use `draw` tool |
| 19 | `service_worker` | Register SW, retrieve from cache | Click "Register", wait, click "Retrieve from Cache" |
| 20 | `dom_mutation` | Trigger 5 DOM mutations | Click "Trigger Mutation" 5x, then "Complete" |
| 21 | `recursive_iframe` | Navigate 3-5 nested iframe levels | `switch_to_iframe` at each level, click "Enter Level N" |
| 22 | `shadow_dom` | Navigate 3 shadow DOM layers | Click "Reveal Code (0/3 levels)" repeatedly |
| 23 | `websocket` | Connect to simulated WS, receive code | Click "Connect", wait for code |

---

## Version × Step Map

Each cell shows the challenge type. **PZL** = math_puzzle with the specific equation.

```
Step |     v1     |     v2     |     v3     |     v4     |     v5     |
──── | ────────── | ────────── | ────────── | ────────── | ────────── |
   1 |   HIDDOM   |   CLICK    |   SCROLL   |   CLICK    |   HIDDOM   |
   2 |   CLICK    |   SCROLL   |   DELAY    |   SCROLL   |   CLICK    |
   3 |   SCROLL   |   DELAY    |  VISIBLE   |   DELAY    |   SCROLL   |
   4 |   DELAY    |  VISIBLE   |   HIDDOM   |  VISIBLE   |   DELAY    |
   5 |  VISIBLE   |   HIDDOM   |   CLICK    |   HIDDOM   |  VISIBLE   |
   6 |    DRAG    |    KEYS    |   MEMORY   |    KEYS    |    DRAG    |
   7 |    KEYS    |   MEMORY   |   HOVER    |   MEMORY   |    KEYS    |
   8 |   MEMORY   |   HOVER    |   CLICK    |   HOVER    |   MEMORY   |
   9 |   HOVER    |   CLICK    |    DRAG    |   CLICK    |   HOVER    |
  10 |   CLICK    |    DRAG    |    KEYS    |    DRAG    |   CLICK    |
  11 |  CAPTURE   |   CANVAS   |   AUDIO    |   CANVAS   |  CAPTURE   |
  12 |   CANVAS   |   AUDIO    |   VIDEO    |   AUDIO    |   CANVAS   |
  13 |   AUDIO    |   VIDEO    |  SCATTER   |   VIDEO    |   AUDIO    |
  14 |   VIDEO    |  SCATTER   |   BASE64   |  SCATTER   |   VIDEO    |
  15 |  SCATTER   |   BASE64   |  CAPTURE   |   BASE64   |  SCATTER   |
  16 |    TABS    |   CANVAS   |   MULTI    |   CANVAS   |    TABS    |
  17 |   CANVAS   |   MULTI    |  PZL 27+7  |   MULTI    |   CANVAS   |
  18 |   MULTI    |  PZL 28+8  |  PZL 28+8  |  PZL 28+8  |   MULTI    |
  19 |  PZL 29+9  |  PZL 29+9  |    TABS    |  PZL 29+9  |  PZL 29+9  |
  20 | PZL 10+10  |    TABS    |   CANVAS   |    TABS    | PZL 10+10  |
  21 |   SHADOW   |   WEBSKT   |   SVCWKR   |   WEBSKT   |   SHADOW   |
  22 |   WEBSKT   |   SVCWKR   |   MUTATE   |   SVCWKR   |   WEBSKT   |
  23 |   SVCWKR   |   MUTATE   |   IFRAME   |   MUTATE   |   SVCWKR   |
  24 |   MUTATE   |   IFRAME   |   MULTI    |   IFRAME   |   MUTATE   |
  25 |   IFRAME   |   MULTI    |    TABS    |   MULTI    |   IFRAME   |
  26 |   MULTI    |    TABS    |   MULTI    |    TABS    |   MULTI    |
  27 |    TABS    |   MULTI    |  PZL 17+17 |   MULTI    |    TABS    |
  28 |   MULTI    |  PZL 18+18 |   SHADOW   |  PZL 18+18 |   MULTI    |
  29 |  PZL 19+19 |   SHADOW   |   WEBSKT   |   SHADOW   |  PZL 19+19 |
  30 |   SHADOW   |   WEBSKT   |   SVCWKR   |   WEBSKT   |   SHADOW   |
```

### Observations

- **v1 ≡ v5** and **v2 ≡ v4** — the versions are symmetric (only 3 truly distinct layouts).
- Challenges 1–10 are the "easy" tier (scroll, click, wait, hover, drag, keys).
- Challenges 11–20 are "medium" (audio, video, canvas, puzzles).
- Challenges 21–30 are "hard" (service worker, websocket, iframe, shadow DOM).
- Math puzzles always appear in the middle band (steps 17–20 or 27–29).

---

## Known Bugs

### Bug 1: Back-to-back math puzzle React state leak

**Affected:** Every version has exactly one pair of adjacent puzzle steps.

| Version | Adjacent Puzzles | State Leak |
|---------|-----------------|------------|
| v1 | Step 19 (29+9) → Step 20 (10+10) | Step 20 shows stale code from 19 |
| v2 | Step 18 (28+8) → Step 19 (29+9) | Step 19 shows stale code from 18 |
| v3 | Step 17 (27+7) → Step 18 (28+8) | Step 18 shows stale code from 17 |
| v4 | Step 18 (28+8) → Step 19 (29+9) | Step 19 shows stale code from 18 |
| v5 | Step 19 (29+9) → Step 20 (10+10) | Step 20 shows stale code from 19 |

**Root cause:** The React puzzle component uses `useState` internally but is not keyed
by step number. When React Router performs client-side navigation to the next step, React
reconciles the same component type in the same tree position and **reuses the existing
state** (solved=true, code=previous_code). The `<input type="number">` and Solve button
are absent; the page shows "Puzzle solved" with the stale code.

**Detection criteria:** On a math puzzle step, if `puzzleSolved=true` AND there is
no `<input type="number">` AND no "Solve" button, AND the visible code was already
submitted in a prior step → the page is in stale state.

**Recovery:** Skip to the next step via `pushState` + `popstate`. The next step is
always a different challenge type (tabs/canvas), so it mounts fresh.

### Bug 2: Unknown (code revealed but submission doesn't advance)

**Status:** Not yet observed in logs. A challenge where the correct code is revealed
and submitted but the page does not advance to the next step. To be identified in
future runs. Potentially related to the same React state issue or a different
component-level bug.

---

## Skip Mechanism

The site uses React Router for client-side navigation. Steps can be skipped via:

```javascript
window.history.pushState({}, '', '/stepN?version=V');
window.dispatchEvent(new PopStateEvent('popstate', { state: {} }));
```

This triggers React Router to re-render the target step without a full page load.
Full `page.goto()` or `window.location.href` causes a Netlify 404.

**Limitations:**
- pushState does NOT reset React component state within the same component type
  (this is why the puzzle state leak persists even with pushState).
- pushState TO a different challenge type DOES mount fresh components.
- Whether skipped steps count as "completed" for the site's progress tracking is
  unclear — the site may track progress client-side only.
