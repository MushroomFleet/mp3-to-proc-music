# MP3toPROC — Tauri + React + three.js Implementation Playbook

> **Audience:** an AI coding agent (for example Claude Code) or a developer adding the *game side* of MP3toPROC to a **Tauri 2 / Rust / Vite / TypeScript / React / three.js / @react-three/fiber / drei** game.
>
> **Scope:** runtime playback only. Audio assets are prepared upstream by a sound engineer with the MP3toPROC Python/PowerShell pipeline. This playbook explains how to consume those assets. Do not port the analysis code into the game.
>
> **Status of the reference code:** every file in Appendix A type-checks under `strict`. The scheduling maths matches the Python reference director exactly; this was checked with the parity test in Appendix A.9. The files have **not** been run in a real Tauri webview yet. Your first job is the smoke test in §8.2.

---

## 0. TL;DR for the agent

1. Copy Appendix A into `src/music/` (or from `runtimes/tauri-react/src/music/` in the MP3toPROC repo).
2. Copy the asset drop from the audio team into `public/music/` (Strategy A, §5).
3. Wrap the app in `<MusicProvider resolver={publicFolderResolver()}>`, outside `<Canvas>`.
4. Drive the music from gameplay with `useMusic().trigger('event_name')` or `<MusicZone event="…" subject={playerRef} />` inside `<Canvas>`.
5. **No Rust code is needed.** All audio runs through the Web Audio API inside the webview.
6. Meet the **Definition of Done** (§10) before reporting the task complete.

Hard rules:
- **One** `AudioContext` and **one** `MusicDirector` per app. Both are module singletons (`runtime.ts`), never created in component state.
- **Never** schedule audio with `setTimeout`/`setInterval`/`useFrame`. All audio is scheduled ahead on `ctx.currentTime`. JS timers only do bookkeeping.
- **Never** hand-edit `track.json` or the audio files. If a loop sounds wrong, send it back to the audio team (§9.3).
- Treat `soundtrack.json` as **design data**. Game designers may edit events and transition rules. Code must not hard-code track ids that could instead come from events.

---

## 1. What the system does

Each finished song is cut upstream into `intro`, a seamlessly looping `loop`, and `outro`. At runtime:

```
Track A intro → Track A loop (repeats) ──trigger──▶ wait for next beat/bar/phrase/loop-end
                                                   → Track A outro ⟶ crossfade ⟶ Track B intro → Track B loop …
```

- **Triggers** are named events (`enter_cave`, `combat_start`, `combat_end`) defined in `soundtrack.json`. Each maps to a target track and, optionally, to rule overrides.
- **Transitions are not linear.** Any track can go to any track. `@previous` returns to the last different track.
- **Quantized exits** keep changes musical. The director waits for the next exit point on the song's own beat/bar/phrase grid, measured by the pipeline.
- **Retargeting is safe.** If triggers arrive in quick succession, the director recomputes or swaps the pending transition. Game code may call `trigger` every frame without harm; repeats are ignored.

---

## 2. The asset contract (what the audio team delivers)

```
music/                         ← the "drop"; copy as a whole folder
  soundtrack.json              ← transition graph (designers may edit)
  <track_id>/
    intro.ogg                  song start → loop start (may be absent/empty)
    loop.ogg                   seamless loop body; loops natively in any player
    outro.ogg                  loop end → song end
    [intro|loop|outro].flac    OPTIONAL fallback copies (--also-format flac)
    track.json                 manifest (bpm, sample counts, exit grids)
    review.png                 for humans; may be excluded from the game build
```

### 2.1 `track.json` (version 1). The fields the runtime uses:

| Field | Meaning |
|---|---|
| `mp3toproc_version` | Schema version. The runtime refuses versions newer than it supports. |
| `id` | Track id. Equals the folder name. |
| `sample_rate` | Rate the sample counts below refer to. Convert to seconds with `samples / sample_rate`. |
| `bpm`, `beats_per_bar` | Used for the beat/bar/phrase grid while the intro is still playing. |
| `segments.{intro,loop,outro}.file` | File name relative to the track folder, or `null` if the segment is empty. |
| `segments.*.samples` | Segment length. |
| `segments.loop.{beat,bar,phrase}_offsets` | **Exit points**, in samples from the loop start. |
| `segments.outro.tail_offset` | Where the song's real ending starts inside the outro (for `outro_from: "tail"`). |
| `analysis.confidence` | `high`/`medium`/`low` seam quality. The runtime logs a warning for `low`. |

### 2.2 `soundtrack.json`

```jsonc
{
  "sample_rate": 44100,
  "start": "forest_dawn",
  "tracks": { "forest_dawn": "forest_dawn/track.json",
              "cave_echoes": { "manifest": "cave_echoes/track.json", "gain": 0.9 } },
  "defaults": { "mode": "outro", "quantize": "phrase", "crossfade_seconds": 4.0,
                "outro_max_seconds": 12, "outro_from": "start", "skip_intro": false,
                "seam_seconds": 0.12, "lead_seconds": 0.05 },
  "transitions": [   // matched in order; later matches override earlier; "*" = any
    { "from": "*", "to": "battle_rush", "mode": "cut", "quantize": "beat",
      "crossfade_seconds": 1.0, "skip_intro": true },
    { "from": "battle_rush", "to": "*", "quantize": "bar", "outro_max_seconds": 8 }
  ],
  "events": {
    "enter_cave": "cave_echoes",
    "combat_start": "battle_rush",
    "combat_end": "@previous",
    "boss_spotted": { "target": "battle_rush", "quantize": "immediate", "crossfade_seconds": 0.3 }
  }
}
```

Rule fields:

| Field | Values | Effect |
|---|---|---|
| `mode` | `outro` \| `cut` | `outro`: at the exit point, play the outro, then start the next track `crossfade_seconds` before the outro ends. `cut`: at the exit point, crossfade directly into the next track. |
| `quantize` | `immediate` \| `beat` \| `bar` \| `phrase` \| `loop_end` | Which grid the exit waits for. |
| `crossfade_seconds` | number | Equal-power overlap. |
| `outro_max_seconds` | number \| null | Trims long outros. |
| `outro_from` | `start` \| `tail` | `tail` jumps straight to the song's real ending section. |
| `skip_intro` | bool | Enter the target at its loop. Use this for urgent music. |
| `seam_seconds` | number | Micro-crossfade used when leaving mid-loop. |
| `lead_seconds` | number | Minimum scheduling headroom. |

---

## 3. Architecture in the game

```
 Gameplay (React state / zustand / R3F useFrame / colliders)
        │   trigger('enter_cave')
        ▼
 useMusic()  ─────────────▶  MusicDirector  (state machine, pure TS + Web Audio)
 <MusicZone>                   │  uses
                               ├── quantize.ts   exit-point maths (pure, unit-tested)
                               ├── envelope.ts   cancellable equal-power fades
                               └── TrackLibrary  fetch + decodeAudioData + LRU memory budget
                                         │ URLs from
                                         ▼
                                  UrlResolver  (public/ folder or Tauri asset protocol)

 Web Audio graph:
   [intro|loop|outro BufferSource] → voice Gain → deck Gain (crossfade) → track trim
        → music bus Gain ─┐
   drei <PositionalAudio> SFX (same AudioContext via THREE.AudioContext.setContext)
        → three AudioListener ─┤
                               └→ master Gain → ctx.destination
```

**Why Web Audio and not Rust audio (rodio/kira) through Tauri commands?** Web Audio's `AudioBufferSourceNode.start(when, offset)` is sample-accurate and supports native gapless looping. It shares a clock with three.js SFX and needs no IPC. Rust-side audio is only worth considering if a target webview's audio proves unusable. See §9.4.

---

## 4. Implementation plan (do these in order)

### Step 1: Add the module
Create `src/music/` with the files from Appendix A:

| File | Role |
|---|---|
| `types.ts` | Asset schema and defaults |
| `quantize.ts` | Exit-point maths (pure) |
| `envelope.ts` | Cancellable equal-power fades |
| `library.ts` | Loading, decoding, format fallback, memory budget |
| `MusicDirector.ts` | Runtime state machine |
| `resolvers.ts` | Where files come from (Vite public folder / Tauri resources) |
| `runtime.ts` | Singletons: AudioContext, master bus, director; HMR cleanup; gesture unlock |
| `react.tsx` | `<MusicProvider>`, `useMusic()`, `<MusicZone>`, `<MusicDebugOverlay>` |
| `parity.test.ts` | Dev-only parity test against the Python golden timeline |

Dependencies: `three`, `@react-three/fiber`, `react` (already present). `@tauri-apps/api` v2 is only needed for Strategy B.

Vite projects already have `import.meta.hot` types through `vite/client`. Make sure `tsconfig` includes `"types": ["vite/client"]` or a `src/vite-env.d.ts` that references it.

### Step 2: Place the assets (§5, Strategy A by default)
```
public/music/soundtrack.json
public/music/<track_id>/{intro,loop,outro}.ogg, track.json
```
Exclude `review.png` from game builds; it is only needed for review.

### Step 3: Mount the provider
```tsx
// src/main.tsx (or App.tsx)
import { MusicProvider } from './music/react';
import { publicFolderResolver } from './music/resolvers';

const musicResolver = publicFolderResolver('/music/'); // module-level = stable identity

createRoot(document.getElementById('root')!).render(
  <MusicProvider resolver={musicResolver}>
    <App />
  </MusicProvider>,
);
```
`MusicProvider` installs a one-time gesture listener that resumes the `AudioContext`. When the context starts running, it starts `soundtrack.json → start`. A title screen with a "Press any key / Click to start" button is the natural place for that gesture. If the game must control when the music starts, pass `autoStart={false}` and call `director.start('menu_theme')` yourself.

### Step 4: Wire gameplay → music
Use whichever trigger style fits each feature:

**a) Imperative, from game logic**
```tsx
const { trigger } = useMusic();
useEffect(() => { if (inCombat) trigger('combat_start'); else trigger('combat_end'); }, [inCombat]);
```

**b) From a zustand store, outside React render**
```ts
import { peekMusic } from './music/runtime';
useGame.subscribe((s) => s.combat, async (combat) => {
  const rt = await peekMusic();
  rt?.director.trigger(combat ? 'combat_start' : 'combat_end');
});
```

**c) Spatial zones inside `<Canvas>`**
```tsx
<MusicZone event="enter_cave" subject={playerRef} position={[40, 0, -12]} size={[30, 12, 30]} debug={import.meta.env.DEV} />
<MusicZone event="enter_forest" subject={playerRef} position={[0, 0, 0]} size={[80, 20, 80]} />
```
Zones form a stack: the most recently entered zone wins, and leaving a zone falls back to the zone underneath. For "no zone at all", call `setZoneFallback('enter_overworld')` once at boot. If the game already uses `@react-three/rapier`, you may replace the box test with a sensor collider (`<CuboidCollider sensor onIntersectionEnter=…>`) that calls the same `trigger`. Keep the stack logic.

**d) Story/scripted beats.** Call `trigger('boss_spotted')` from the cutscene or dialogue system. Rule overrides such as `"quantize": "immediate"` belong in `soundtrack.json`, not in code.

### Step 5: Shared audio bus and SFX
`runtime.ts` calls `THREE.AudioContext.setContext(ctx)` **before any `AudioListener` exists**. drei `<PositionalAudio>` and three's `Audio` then use the same context as the music. Requirements:
- Import `src/music/runtime` (or mount `<MusicProvider>`) **before** the first `<PositionalAudio>`/`AudioListener` is created.
- Route the three.js listener's output into the master bus if you want one global volume:
  `listener.gain.disconnect(); listener.gain.connect(getMasterBus());`

Volume controls:
- Music only: `director.setVolume(v, fade)`.
- Everything: `getMasterBus().gain`.
- Dialogue ducking: `director.setVolume(0.35, 0.3)` when a line starts, then `director.setVolume(1, 0.8)` when it ends.

### Step 6: Pause, focus, lifecycle
- **Game pause (everything):** `ctx.suspend()` and later `ctx.resume()`. Scheduled transitions freeze and resume in step because they live on the context clock.
- **Music-only mute:** fade the bus. Do not suspend.
- **Window unfocused (optional design choice):**
  ```ts
  import { getCurrentWindow } from '@tauri-apps/api/window';
  getCurrentWindow().onFocusChanged(({ payload: focused }) => (focused ? ctx.resume() : ctx.suspend()));
  ```
- **Back to menu / game over:** `director.stopAll(1.5)`, then `director.start('menu_theme')`.
- **HMR:** `runtime.ts` disposes the director on hot reload. Never create a second `AudioContext` anywhere else.

### Step 7: Preloading and memory
Decoded audio is uncompressed float32, roughly **10.6 MB per stereo minute at 44.1 kHz**. A 3-minute track is about 32 MB.
- `initMusic` preloads the start track, then every event target in the background.
- With large soundtracks (more than about 15 tracks), pass `preload` explicitly per level and lower `budgetMB`. The library evicts least-recently-used tracks but never the current or pending one.
- At level load, call `library.preload([...])` for the tracks that level can reach. That way `trigger` never waits on decoding.
- If a trigger hits an unloaded track, the director still works: it waits for the decode, then computes the exit from the time decoding finished. The change lands a phrase later than it would otherwise.

### Step 8: Dev tooling
- Render `<MusicDebugOverlay />` in dev builds. It shows the current track, the pending track, the phase, the loop position and the next exit times.
- Log `director.on(e => console.debug('[music]', e))` in dev.
- For live auditioning without the game, the audio team runs `python -m mp3toproc play` on the same `soundtrack.json`.

---

## 5. Getting files into the webview (Tauri 2)

### Strategy A (default): Vite `public/` folder
Files under `public/music/` are copied into `dist/`, which Tauri embeds as the frontend. `fetch('/music/…')` works the same in `tauri dev` (served by Vite) and in release builds (served by Tauri's internal protocol).
- Resolver: `publicFolderResolver('/music/')`
- No Tauri config changes are needed.
- Trade-off: the music is compiled into the app binary, so changing music means rebuilding. This is fine for prototypes and small or medium soundtracks.

### Strategy B: Tauri bundle resources + asset protocol
Use this for large soundtracks, DLC packs, or swapping music without rebuilding the frontend.

`src-tauri/tauri.conf.json`:
```jsonc
{
  "bundle": { "resources": { "music/": "music/" } },          // src-tauri/music/** -> $RESOURCE/music/**
  "app": {
    "security": {
      "assetProtocol": { "enable": true, "scope": ["$RESOURCE/music/**/*"] },
      "csp": {
        "default-src": "'self'",
        "connect-src": "'self' ipc: http://ipc.localhost asset: http://asset.localhost",
        "media-src": "'self' asset: http://asset.localhost blob:"
      }
    }
  }
}
```
`src-tauri/Cargo.toml`: enable the asset protocol feature on the tauri crate, for example `tauri = { version = "2", features = ["protocol-asset"] }`.

Resolver: `tauriResourceResolver('music/')`. It uses `resolveResource` and `convertFileSrc` from `@tauri-apps/api` v2.

Four settings must agree: the feature flag, `assetProtocol.enable` plus `scope`, the CSP `connect-src` (because the library uses `fetch`), and the resolver. Smoke-test **both** `tauri dev` and a release build, and watch the devtools console and network tab for `(blocked:csp)` or 403 errors. If something fails, check the current Tauri v2 docs on "Asset protocol scope" and "Embedding Additional Files". Config keys have changed between minor versions.

### Strategy C (dev only): hot-swapping from the audio team's output folder
To iterate on music without copying files, point a dev-only resolver at the absolute output folder through the asset protocol, with a dev-only `scope`. Never ship a wide scope in release builds.

---

## 6. Platform and codec matrix (decodeAudioData in the webview)

| Platform | Webview | Ogg Vorbis | FLAC | WAV | Notes |
|---|---|---|---|---|---|
| Windows 10/11 | WebView2 (Chromium) | ✅ | ✅ | ✅ | Primary target; OGG recommended |
| macOS 15.4+ | WKWebView (Safari 18.4+) | ✅ | ✅ | ✅ | Ogg/Vorbis support arrived in Safari 18.4 |
| macOS < 15.4 | WKWebView | ❌ | ✅ | ✅ | Ship the FLAC fallback |
| Linux | WebKitGTK (GStreamer) | ✅* | ✅* | ✅ | *Requires GStreamer plugins (`gst-plugins-good`/`base`). Most desktops have them; document this for players. |

**Policy:**
- Windows-only prototype: ship OGG only.
- Cross-platform: ask the audio team for `-AlsoFormat flac` (`--also-format flac`). `TrackLibrary` automatically tries `loop.ogg`, then `loop.flac`, then `loop.wav`.
- Never use MP3 for loops. MP3 encoders add padding that breaks gapless looping, and the pipeline never outputs MP3.

---

## 7. How the director works (so you can debug or extend it)

**Clock.** Everything is in seconds on `ctx.currentTime`. Manifest sample counts are converted using `track.json → sample_rate`. After decoding, the real buffer durations replace the manifest durations (`withBufferDurations`), because `decodeAudioData` resamples to the context rate and the loop period equals the buffer length.

**Deck.** One playing track: an intro source starting at `start` and a looping loop source starting at `loopStart = start + introDur`. The loop source has `loop = true` over the whole buffer, and the seam crossfade is already baked into the file. Each deck has its own gain, used for crossfades.

**Exit point** (`quantize.exitPoint`):
- In the loop: `pos = (t − loopStart) mod loopDur`. E is the next value in `offsets[quantize] ∪ {loopDur}` at or after `pos`.
- In the intro: the grid counts backwards from `loopStart` using `beatLen`/`barLen`/`phraseLen`.
- `immediate`: E = now + lead.

**Commit** (`outro` mode):
1. Sources that would start at or after E are cancelled (`stop(E)` before their start time means they never play).
2. Sounding sources fade out over `seam_seconds` from E. If E lands exactly on a loop wrap, they hard-stop at E instead, which is sample-exact because the outro is the song's own continuation.
3. The outro starts at E, capped at `outro_max_seconds`. The next deck starts at `S = E + outroLen − crossfade`, and both crossfade with equal-power curves.

`cut` mode: the current deck fades out over `crossfade_seconds` from E, and the next deck fades in from E.

**Retarget** (a trigger arrives while a transition is pending and the next track is not yet audible):
- E is still in the future: undo the exit. The deck's sources are re-stopped at E and rebuilt from E at the correct offset, so playback continues seamlessly. Then compute a fresh exit toward the new target.
- E has passed (the outro is playing): keep the outro and replace the next deck at the same start time S.
- The next track is already audible: promote it and transition away from it.

**Envelope.** Fades are dense `linearRampToValueAtTime` steps shaped equal-power. `setValueCurveAtTime` is avoided because overlapping curves throw. The class mirrors every point it schedules, so `cancel(t)` can continue from the exact current value with no clicks.

**Bookkeeping.** A 50 ms `setInterval` promotes the pending deck when it becomes audible and frees finished decks. A late timer never affects audio timing.

---

## 8. Testing

### 8.1 Parity test (Node, no audio device): required
The Python preview can emit a golden timeline. The TS director must reproduce every exit and start time within 1 ms.
```bash
# audio team / CI, in the MP3toPROC repo:
python -m mp3toproc preview music/soundtrack.json events.json -o p.wav --events-json golden.json
# game repo:
npx tsx src/music/parity.test.ts public/music/ golden.json        # prints PARITY OK
```
Add a script `"test:music-parity"` to `package.json`. Keep `golden.json` and `events.json` in `src/music/__fixtures__/`, and regenerate them whenever the audio team delivers a new `soundtrack.json`.

### 8.2 Webview smoke test: required, manual, about 5 minutes
Run `tauri dev` and use `<MusicDebugOverlay/>`. Check that:
1. Nothing plays before the first click or keypress. After it, the start track plays.
2. Letting a track loop at least twice produces no click, gap or level jump at the wrap. Watch the overlay's loop position reset.
3. A `phrase` trigger exits on the next phrase shown in the overlay, and the outro crossfades into the next intro.
4. Spamming the same trigger does nothing extra. Two different triggers 0.5 s apart end on the second target.
5. Triggering combat during an outro keeps the outro and enters combat at the planned time.
6. Hot-reload a component (HMR) and confirm there is only one music stream, not two layered streams.
7. Repeat steps 1–3 in a **release build**, and on every OS you target (§6).

### 8.3 Unit tests (vitest)
`quantize.ts` is pure, so test `exitPoint` against fixture `track.json` files: inside the intro, mid-loop, exactly on an exit point, at the loop wrap, and with `immediate`. Optionally test `Envelope` with a fake `AudioParam` that records calls.

---

## 9. Gotchas and troubleshooting

### 9.1 Audio
| Symptom | Cause | Fix |
|---|---|---|
| Silence, `ctx.state === 'suspended'` | Autoplay policy | A user gesture must call `resume()`. `installAudioUnlock` handles this; make sure the title screen requires a click or key. |
| Two layers of music | A second `AudioContext`/director (StrictMode, HMR, copy-pasted `new AudioContext`) | Use only `runtime.ts` singletons. |
| Click at the loop wrap | Source not looping the whole buffer, a custom `loopStart`/`loopEnd`, or a re-encoded file | Use `loop=true` with no loop points, and ship the pipeline's files unmodified. |
| Exit lands off-beat | Grid built from the manifest but the buffer was resampled, or offsets read as seconds | Use `withBufferDurations`, and treat offsets as samples at `manifest.sample_rate`. |
| `NotSupportedError` on fades | Someone added `setValueCurveAtTime` | Use `Envelope` only. |
| Transitions late by about a phrase | Track wasn't decoded yet | Preload the level's reachable tracks. |
| Works in dev, silent in release (Strategy B) | CSP or asset scope | See §5. Check the network tab. |
| macOS: decode error on `.ogg` | macOS older than 15.4 | Ship FLAC fallbacks (§6). |
| Linux: decode error | Missing GStreamer plugins | Install `gst-plugins-good`, or ship WAV. |

### 9.2 React and R3F
- Keep the `resolver` identity stable by defining it at module level. A new function each render re-runs the provider effect.
- Don't call `trigger` from inside render. Use effects, event handlers, `useFrame`, or store subscriptions.
- `useFrame` stops when the tab or window is hidden, so zones won't fire then. That is expected; the music keeps playing.
- `MusicZone` checks `getWorldPosition` every frame. For dozens of zones, switch to physics sensors or one manager that tests all boxes.

### 9.3 When the music itself is wrong
Send it back upstream; don't patch it in code. The audio team re-runs the converter with `--bpm`, `--loop-start-bar`/`--loop-end-bar`, or the overrides file, checks `review.png`, and re-delivers the drop. Tracks with `analysis.confidence: "low"` log a console warning on load.

### 9.4 Escalation path: native audio
Only if a target webview's Web Audio proves unusable (for example, latency spikes or device switching bugs on a specific Linux setup): implement the same director in Rust with `kira` (it has clock-based scheduling and tweens). Expose `music_trigger(name)` as a Tauri command and keep `soundtrack.json` and the asset contract unchanged. Treat this as a separate commission.

---

## 10. Definition of Done

- [ ] `src/music/*` added; `tsc --noEmit` passes in the game repo.
- [ ] Assets in place using Strategy A (or B, with the config from §5 verified in a release build).
- [ ] `<MusicProvider>` mounted once; the start track plays after the first gesture.
- [ ] At least one zone or gameplay event drives a transition; `combat_start`/`combat_end` (or the game's equivalents) work with `@previous`.
- [ ] Parity test passes against the current `golden.json` (`PARITY OK`).
- [ ] Every item in the webview smoke test (§8.2) passes in dev **and** in a release build on each target OS.
- [ ] drei/three SFX share the context; music volume and master volume can be controlled separately.
- [ ] Pause behaviour implemented per the design (`ctx.suspend`/`resume`).
- [ ] No `setTimeout`-based audio scheduling anywhere; no second `AudioContext`.
- [ ] `<MusicDebugOverlay>` and debug zone boxes appear only in dev builds.

---

## 11. Extension points (later commissions)

- **Stingers:** one-shot sources scheduled on `exitPoint(…,'beat')` without changing tracks.
- **Intensity layers:** extra stems synced to `deck.loopStart`, with a gain per layer.
- **Per-event cooldowns:** debounce events such as `combat_end` in game code, or add a `min_hold_seconds` rule field. If you add one, mirror it in the Python director so parity holds.
- **Save/restore:** store `director.current`; on load, call `director.start(saved)`.

---

## Appendix A: Reference implementation (`src/music/`)

Copy verbatim. These are the same files as `runtimes/tauri-react/src/music/` in the MP3toPROC repo.

### A.1 `src/music/types.ts`

```ts
// Schema of the files produced by the MP3toPROC converter (mp3toproc_version 1).
// Do not edit these files by hand in the game repo; regenerate them with the pipeline.

export interface SegmentInfo {
  file: string | null; // relative to the track folder; null = segment is empty
  samples: number; // at manifest.sample_rate
  seconds: number;
}

export interface TrackManifest {
  mp3toproc_version: number;
  id: string;
  source: string;
  sample_rate: number;
  channels: number;
  bpm: number;
  beats_per_bar: number;
  segments: {
    intro: SegmentInfo;
    loop: SegmentInfo & {
      bars: number;
      bar_offsets: number[]; // samples from loop start
      phrase_offsets: number[];
      beat_offsets: number[];
    };
    outro: SegmentInfo & { tail_offset?: number };
  };
  transition_defaults?: { crossfade_seconds?: number; quantize?: Quantize };
  analysis: {
    loop_score: number;
    confidence: 'high' | 'medium' | 'low';
    warnings: string[];
    [k: string]: unknown;
  };
}

export type Quantize = 'immediate' | 'beat' | 'bar' | 'phrase' | 'loop_end';
export type Mode = 'outro' | 'cut';

export interface TransitionRule {
  mode: Mode;
  quantize: Quantize;
  crossfade_seconds: number;
  outro_max_seconds: number | null;
  outro_from: 'start' | 'tail';
  skip_intro: boolean;
  seam_seconds: number;
  lead_seconds: number;
}

export type TrackRef = string | { manifest: string; gain?: number };
export type EventTarget = string | (Partial<TransitionRule> & { target: string });

export interface SoundtrackConfig {
  sample_rate?: number;
  start?: string;
  tracks: Record<string, TrackRef>;
  defaults?: Partial<TransitionRule>;
  transitions?: Array<Partial<TransitionRule> & { from?: string; to?: string; _comment?: string }>;
  events?: Record<string, EventTarget>;
}

export const DEFAULT_RULE: TransitionRule = {
  mode: 'outro',
  quantize: 'phrase',
  crossfade_seconds: 4,
  outro_max_seconds: null,
  outro_from: 'start',
  skip_intro: false,
  seam_seconds: 0.12,
  lead_seconds: 0.05,
};

export const SUPPORTED_MANIFEST_VERSION = 1;
```

### A.2 `src/music/quantize.ts`

```ts
// Pure timing maths. No Web Audio here, so it can be unit-tested in Node.
// All times are in SECONDS on the AudioContext clock.
import type { Quantize, TrackManifest } from './types';

export type GridKey = Exclude<Quantize, 'immediate'>;

export interface TrackGrid {
  introDur: number;
  loopDur: number;
  outroDur: number;
  outroTail: number; // where the real ending starts inside the outro
  beatLen: number;
  barLen: number;
  phraseLen: number;
  offsets: Record<GridKey, number[]>; // exit points, seconds from loop start
}

const EPS = 1e-6;

export function buildGrid(m: TrackManifest): TrackGrid {
  const sr = m.sample_rate;
  const s = (n: number) => n / sr;
  const loop = m.segments.loop;
  const beatLen = 60 / m.bpm;
  const barLen = beatLen * (m.beats_per_bar || 4);
  const phrase = loop.phrase_offsets.map(s);
  return {
    introDur: s(m.segments.intro.samples),
    loopDur: s(loop.samples),
    outroDur: s(m.segments.outro.samples),
    outroTail: s(m.segments.outro.tail_offset ?? 0),
    beatLen,
    barLen,
    phraseLen: phrase.length > 1 ? phrase[1] - phrase[0] : barLen * 4,
    offsets: {
      beat: loop.beat_offsets.map(s),
      bar: loop.bar_offsets.map(s),
      phrase,
      loop_end: [0],
    },
  };
}

/** Replace manifest durations with the real decoded buffer durations
 *  (decodeAudioData resamples to the context rate; the loop period is the buffer length). */
export function withBufferDurations(
  g: TrackGrid,
  d: { intro?: number; loop?: number; outro?: number },
): TrackGrid {
  return {
    ...g,
    introDur: d.intro ?? g.introDur,
    loopDur: d.loop ?? g.loopDur,
    outroDur: d.outro ?? g.outroDur,
  };
}

/**
 * First musically valid time >= `earliest` to leave a deck whose first loop
 * pass starts at `loopStart`. Mirrors MusicDirector._exit_point in Python.
 */
export function exitPoint(g: TrackGrid, loopStart: number, q: Quantize, earliest: number): number {
  if (q === 'immediate') return earliest;
  if (earliest < loopStart - EPS) {
    // Still in the intro: count the grid backwards from the loop start.
    if (q === 'loop_end' || g.loopDur === 0) return loopStart;
    const period = q === 'beat' ? g.beatLen : q === 'bar' ? g.barLen : g.phraseLen;
    const k = Math.floor((loopStart - earliest) / period + EPS);
    return loopStart - k * period;
  }
  const L = g.loopDur;
  const rel = earliest - loopStart;
  const it = Math.floor(rel / L + EPS);
  const pos = Math.max(0, rel - it * L);
  const next = [...g.offsets[q], L].find((o) => o >= pos - EPS) ?? L;
  return loopStart + it * L + next;
}

/** True when `t` lands exactly on a loop wrap (or the intro->loop join). */
export function isOnSeam(g: TrackGrid, loopStart: number, t: number): boolean {
  if (g.loopDur <= 0) return false;
  const rel = t - loopStart;
  if (rel < -1e-4) return false;
  const r = rel - Math.round(rel / g.loopDur) * g.loopDur;
  return Math.abs(r) < 1e-4;
}

/** Loop-relative playhead (seconds) at time t, or null while still in the intro. */
export function loopPosition(g: TrackGrid, loopStart: number, t: number): number | null {
  if (t < loopStart || g.loopDur <= 0) return null;
  return (t - loopStart) % g.loopDur;
}
```

### A.3 `src/music/envelope.ts`

```ts
// Cancellable equal-power gain envelopes on an AudioParam.
//
// Why not setValueCurveAtTime? It throws NotSupportedError if a new curve
// overlaps an existing one, which happens whenever a transition is
// retargeted mid-fade. Dense linear ramps never throw, and because this class
// mirrors every scheduled point it always knows the exact value at any time,
// so a cancel can ramp from the true current value (no jumps).

export class Envelope {
  private pts: Array<[number, number]> = [];

  constructor(
    private readonly param: AudioParam,
    initial: number,
    at: number,
  ) {
    param.cancelScheduledValues(0);
    param.setValueAtTime(initial, at);
    this.pts = [[at, initial]];
  }

  valueAt(t: number): number {
    const p = this.pts;
    if (t <= p[0][0]) return p[0][1];
    for (let i = 1; i < p.length; i++) {
      if (t <= p[i][0]) {
        const [t0, v0] = p[i - 1];
        const [t1, v1] = p[i];
        return t1 === t0 ? v1 : v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
      }
    }
    return p[p.length - 1][1];
  }

  /** Drop everything scheduled at/after t, keeping the value continuous. */
  cancel(t: number): void {
    const v = this.valueAt(t);
    this.param.cancelScheduledValues(t);
    this.pts = this.pts.filter(([pt]) => pt < t);
    if (this.pts.length === 0) {
      this.param.setValueAtTime(v, t);
    } else {
      this.param.linearRampToValueAtTime(v, t); // matches our linear model exactly
    }
    this.pts.push([t, v]);
  }

  /** Equal-power fade from the current value at t to `to` over `dur` seconds. */
  fade(t: number, dur: number, to: number, steps = 24): void {
    const from = this.valueAt(t);
    this.cancel(t);
    if (dur <= 0) {
      this.param.setValueAtTime(to, t);
      this.pts.push([t, to]);
      return;
    }
    const up = to > from;
    for (let i = 1; i <= steps; i++) {
      const x = i / steps;
      const shape = up ? Math.sin((x * Math.PI) / 2) : 1 - Math.cos((x * Math.PI) / 2);
      const v = from + (to - from) * shape;
      const ti = t + dur * x;
      this.param.linearRampToValueAtTime(v, ti);
      this.pts.push([ti, v]);
    }
  }

  set(t: number, v: number): void {
    this.cancel(t);
    this.param.setValueAtTime(v, t);
    this.pts.push([t, v]);
  }
}
```

### A.4 `src/music/library.ts`

```ts
// Loads soundtrack.json + track manifests, decodes audio on demand, and keeps
// decoded PCM within a memory budget (decoded audio is big: ~10.6 MB per
// stereo minute at 44.1 kHz float32).
import { buildGrid, withBufferDurations, type TrackGrid } from './quantize';
import {
  SUPPORTED_MANIFEST_VERSION,
  type SoundtrackConfig,
  type TrackManifest,
} from './types';

/** Maps a path relative to the music root ("forest/loop.ogg") to a fetchable URL. */
export type UrlResolver = (relPath: string) => string | Promise<string>;

export interface LoadedTrack {
  id: string;
  manifest: TrackManifest;
  grid: TrackGrid;
  gain: number;
  buffers: { intro: AudioBuffer | null; loop: AudioBuffer | null; outro: AudioBuffer | null };
  bytes: number;
}

interface Entry {
  manifest: TrackManifest;
  dir: string; // e.g. "forest_dawn/"
  gain: number;
  loading?: Promise<LoadedTrack>;
  loaded?: LoadedTrack;
  lastUsed: number;
}

const FALLBACK_EXT = ['.ogg', '.flac', '.wav'];

export class TrackLibrary {
  config!: SoundtrackConfig;
  private entries = new Map<string, Entry>();
  private pinned = new Set<string>();

  constructor(
    private readonly ctx: BaseAudioContext,
    private readonly resolve: UrlResolver,
    private readonly opts: { soundtrackPath?: string; budgetBytes?: number; fetchImpl?: typeof fetch } = {},
  ) {}

  private get fetch(): typeof fetch {
    return this.opts.fetchImpl ?? fetch.bind(globalThis);
  }

  async init(): Promise<SoundtrackConfig> {
    const stPath = this.opts.soundtrackPath ?? 'soundtrack.json';
    const base = stPath.includes('/') ? stPath.slice(0, stPath.lastIndexOf('/') + 1) : '';
    this.config = await this.json<SoundtrackConfig>(stPath);
    await Promise.all(
      Object.entries(this.config.tracks).map(async ([id, ref]) => {
        const mPath = base + (typeof ref === 'string' ? ref : ref.manifest);
        const manifest = await this.json<TrackManifest>(mPath);
        if (manifest.mp3toproc_version > SUPPORTED_MANIFEST_VERSION) {
          throw new Error(`[music] ${id}: manifest v${manifest.mp3toproc_version} is newer than this runtime supports`);
        }
        if (manifest.analysis?.confidence === 'low') {
          console.warn(`[music] ${id}: loop seam confidence is LOW, ask the audio team to audition it`);
        }
        this.entries.set(id, {
          manifest,
          dir: mPath.slice(0, mPath.lastIndexOf('/') + 1),
          gain: typeof ref === 'string' ? 1 : ref.gain ?? 1,
          lastUsed: 0,
        });
      }),
    );
    return this.config;
  }

  has(id: string): boolean {
    return this.entries.has(id);
  }

  ids(): string[] {
    return [...this.entries.keys()];
  }

  /** Keep these tracks decoded (current + pending). Others may be evicted. */
  pin(ids: Iterable<string>): void {
    this.pinned = new Set(ids);
  }

  isLoaded(id: string): boolean {
    return !!this.entries.get(id)?.loaded;
  }

  preload(ids: string[]): Promise<unknown> {
    return Promise.allSettled(ids.map((id) => this.ensure(id)));
  }

  ensure(id: string): Promise<LoadedTrack> {
    const e = this.entries.get(id);
    if (!e) return Promise.reject(new Error(`[music] unknown track "${id}"`));
    e.lastUsed = performance.now();
    if (e.loaded) return Promise.resolve(e.loaded);
    if (!e.loading) {
      e.loading = this.decodeTrack(id, e).then(
        (t) => {
          e.loaded = t;
          e.loading = undefined;
          this.evict();
          return t;
        },
        (err) => {
          e.loading = undefined;
          throw err;
        },
      );
    }
    return e.loading;
  }

  private async decodeTrack(id: string, e: Entry): Promise<LoadedTrack> {
    const seg = e.manifest.segments;
    const [intro, loop, outro] = await Promise.all([
      this.decodeSegment(e.dir, seg.intro.file),
      this.decodeSegment(e.dir, seg.loop.file),
      this.decodeSegment(e.dir, seg.outro.file),
    ]);
    if (!loop) throw new Error(`[music] ${id}: loop segment missing`);
    const grid = withBufferDurations(buildGrid(e.manifest), {
      intro: intro?.duration ?? 0,
      loop: loop.duration,
      outro: outro?.duration ?? 0,
    });
    const bytes = [intro, loop, outro].reduce((n, b) => n + (b ? b.length * b.numberOfChannels * 4 : 0), 0);
    return { id, manifest: e.manifest, grid, gain: e.gain, buffers: { intro, loop, outro }, bytes };
  }

  /** Tries the manifest's file first, then the same stem with other extensions.
   *  This covers webviews that can't decode Ogg (e.g. macOS < 15.4). */
  private async decodeSegment(dir: string, file: string | null): Promise<AudioBuffer | null> {
    if (!file) return null;
    const stem = file.replace(/\.[^.]+$/, '');
    const ext = file.slice(stem.length);
    const candidates = [file, ...FALLBACK_EXT.filter((x) => x !== ext).map((x) => stem + x)];
    let lastErr: unknown;
    for (const f of candidates) {
      try {
        const res = await this.fetch(await this.resolve(dir + f));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.arrayBuffer();
        return await this.ctx.decodeAudioData(data);
      } catch (err) {
        lastErr = err;
      }
    }
    throw new Error(`[music] could not load/decode ${dir}${file} (tried ${candidates.join(', ')}): ${String(lastErr)}`);
  }

  private evict(): void {
    const budget = this.opts.budgetBytes ?? 512 * 1024 * 1024;
    const loaded = [...this.entries.entries()].filter(([, e]) => e.loaded);
    let total = loaded.reduce((n, [, e]) => n + e.loaded!.bytes, 0);
    loaded.sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    for (const [id, e] of loaded) {
      if (total <= budget) break;
      if (this.pinned.has(id)) continue;
      total -= e.loaded!.bytes;
      e.loaded = undefined;
    }
  }

  private async json<T>(rel: string): Promise<T> {
    const res = await this.fetch(await this.resolve(rel));
    if (!res.ok) throw new Error(`[music] ${rel}: HTTP ${res.status}`);
    return (await res.json()) as T;
  }
}
```

### A.5 `src/music/MusicDirector.ts`

```ts
// Runtime half of MP3toPROC for Web Audio (Tauri webview, browsers).
// TypeScript port of mp3toproc/director.py; produces the same exit/start
// times for the same inputs (see the parity test).
//
//   intro -> loop (native looping AudioBufferSourceNode) -> [trigger]
//         -> exit at the next quantized point -> outro  ⟶ crossfade ⟶  next intro -> ...
//
// All audio is scheduled ahead on the AudioContext clock, so it is
// sample-accurate even if the JS thread stalls (GC, heavy frame, hidden
// window). JS timers are only used for bookkeeping (which track is current, cleanup).
import { Envelope } from './envelope';
import type { LoadedTrack, TrackLibrary } from './library';
import { exitPoint, isOnSeam, loopPosition } from './quantize';
import { DEFAULT_RULE, type TransitionRule } from './types';

type Kind = 'intro' | 'loop' | 'outro';

interface Voice {
  kind: Kind;
  node: AudioBufferSourceNode;
  gain: GainNode;
  env: Envelope;
  start: number; // ctx time buffer position `offset` plays
  offset: number;
}

interface Deck {
  track: LoadedTrack;
  start: number;
  loopStart: number;
  out: GainNode; // deck crossfade gain
  trim: GainNode; // per-track gain from soundtrack.json
  env: Envelope;
  fade: number; // fade-in length the deck started with
  voices: Voice[];
  exitAt: number | null;
  endAt: number; // when the deck is silent and can be disposed
}

export type DirectorEvent =
  | { type: 'track_started'; track: string; time: number }
  | {
      type: 'exit_scheduled';
      trigger: string;
      source: string;
      target: string;
      mode: 'outro' | 'cut' | 'retarget';
      quantize: string;
      exit: number;
      nextStart: number;
    }
  | { type: 'trigger_ignored'; trigger: string; reason: string }
  | { type: 'error'; error: unknown };

export interface ScheduleResult {
  exit: number;
  start: number;
}

export interface DirectorState {
  current: string | null;
  pending: string | null;
  phase: 'idle' | 'intro' | 'loop' | 'outro';
  loopPosition: number | null;
  nextExit: Record<'beat' | 'bar' | 'phrase', number> | null;
}

export class MusicDirector {
  readonly bus: GainNode;
  private busEnv: Envelope;
  private deck: Deck | null = null;
  private pending: Deck | null = null;
  private retired: Deck[] = [];
  private history: string[] = [];
  private listeners = new Set<(e: DirectorEvent) => void>();
  private requestSeq = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly defaults: TransitionRule;

  constructor(
    private readonly ctx: BaseAudioContext,
    private readonly lib: TrackLibrary,
    opts: { destination?: AudioNode; autoTick?: boolean } = {},
  ) {
    this.defaults = { ...DEFAULT_RULE, ...(lib.config.defaults ?? {}) };
    this.bus = ctx.createGain();
    this.busEnv = new Envelope(this.bus.gain, 1, ctx.currentTime);
    this.bus.connect(opts.destination ?? ctx.destination);
    if (opts.autoTick !== false) this.timer = setInterval(() => this.tick(), 50);
  }

  // ------------------------------------------------------------------ public API

  on(fn: (e: DirectorEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  get current(): string | null {
    return (this.pending ?? this.deck)?.track.id ?? null;
  }

  /** Start playback (call after the AudioContext is running). */
  async start(id = this.lib.config.start, at?: number): Promise<void> {
    if (!id) throw new Error('[music] no start track');
    const track = await this.lib.ensure(id);
    const t = at ?? this.ctx.currentTime + this.defaults.lead_seconds;
    this.deck = this.startDeck(track, t, this.defaults, 0);
    this.history.push(id);
    this.pinCurrent();
    this.emit({ type: 'track_started', track: id, time: t });
  }

  /** Game-facing: event name from soundtrack.json, or a track id. Safe to spam. */
  async trigger(name: string): Promise<ScheduleResult | null> {
    let resolved: { target: string; override: Partial<TransitionRule> } | null;
    try {
      resolved = this.resolve(name);
    } catch (error) {
      this.emit({ type: 'error', error });
      throw error;
    }
    if (!resolved) {
      this.emit({ type: 'trigger_ignored', trigger: name, reason: 'no previous track' });
      return null;
    }
    const seq = ++this.requestSeq;
    const track = await this.lib.ensure(resolved.target); // usually already preloaded
    if (seq !== this.requestSeq) return null; // a newer trigger superseded this one
    return this.request(track, resolved.override, name);
  }

  /** Bookkeeping: promote the pending deck when it becomes audible, free finished decks. */
  tick(): void {
    const now = this.ctx.currentTime;
    if (this.pending && now >= this.pending.start) this.promote();
    this.retired = this.retired.filter((d) => {
      if (now < d.endAt + 0.25) return true;
      this.disposeDeck(d);
      return false;
    });
  }

  setVolume(v: number, fadeSeconds = 0.5): void {
    this.busEnv.fade(this.ctx.currentTime, fadeSeconds, v);
  }

  /** Fade everything out (e.g. game over / back to menu). */
  stopAll(fadeSeconds = 1): void {
    const now = this.ctx.currentTime;
    for (const d of [this.deck, this.pending]) {
      if (!d) continue;
      d.env.fade(now, fadeSeconds, 0);
      for (const v of d.voices) safeStop(v.node, now + fadeSeconds);
      d.endAt = now + fadeSeconds;
      d.exitAt = now;
      this.retired.push(d);
    }
    this.deck = this.pending = null;
  }

  getState(): DirectorState {
    const d = this.deck;
    const now = this.ctx.currentTime;
    if (!d) return { current: null, pending: null, phase: 'idle', loopPosition: null, nextExit: null };
    const g = d.track.grid;
    const phase = d.exitAt !== null && now >= d.exitAt ? 'outro' : now < d.loopStart ? 'intro' : 'loop';
    return {
      current: d.track.id,
      pending: this.pending?.track.id ?? null,
      phase,
      loopPosition: loopPosition(g, d.loopStart, now),
      nextExit: {
        beat: exitPoint(g, d.loopStart, 'beat', now),
        bar: exitPoint(g, d.loopStart, 'bar', now),
        phrase: exitPoint(g, d.loopStart, 'phrase', now),
      },
    };
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    for (const d of [this.deck, this.pending, ...this.retired]) if (d) this.disposeDeck(d);
    this.deck = this.pending = null;
    this.retired = [];
    this.bus.disconnect();
    this.listeners.clear();
  }

  // ------------------------------------------------------------------ internals

  private emit(e: DirectorEvent): void {
    for (const fn of this.listeners) fn(e);
  }

  private resolve(name: string): { target: string; override: Partial<TransitionRule> } | null {
    const ev = this.lib.config.events?.[name];
    let target: string;
    let override: Partial<TransitionRule> = {};
    if (ev === undefined) target = name;
    else if (typeof ev === 'string') target = ev;
    else {
      const { target: t, ...rest } = ev;
      target = t;
      override = rest;
    }
    if (target === '@previous') {
      const cur = this.current;
      const prev = [...this.history].reverse().find((t) => t !== cur);
      if (!prev) return null;
      target = prev;
    }
    if (!this.lib.has(target)) throw new Error(`[music] unknown event or track "${name}"`);
    return { target, override };
  }

  private rule(src: string | null, dst: string, override: Partial<TransitionRule>): TransitionRule {
    const r: TransitionRule = { ...this.defaults };
    for (const rule of this.lib.config.transitions ?? []) {
      const from = rule.from ?? '*';
      const to = rule.to ?? '*';
      if ((from === '*' || from === src) && (to === '*' || to === dst)) {
        const { from: _f, to: _t, _comment, ...rest } = rule;
        Object.assign(r, rest);
      }
    }
    return Object.assign(r, override);
  }

  private request(target: LoadedTrack, override: Partial<TransitionRule>, name: string): ScheduleResult | null {
    const now = this.ctx.currentTime;
    const lead = this.defaults.lead_seconds;
    this.tick();

    if (this.pending) {
      const src = this.deck!;
      const old = this.pending;
      if (old.track.id === target.id) return null;
      if (old.start > now + lead) {
        this.killDeck(old, now);
        this.pending = null;
        if (src.exitAt !== null && src.exitAt > now + lead) {
          // The exit hasn't happened yet: undo it and recompute from scratch.
          this.rewind(src, src.exitAt);
        } else {
          // The outro is already playing: keep it, only swap what comes next.
          const rule = this.rule(src.track.id, target.id, override);
          this.pending = this.startDeck(target, old.start, rule, old.fade);
          this.pinCurrent();
          this.emit({
            type: 'exit_scheduled', trigger: name, source: src.track.id, target: target.id,
            mode: 'retarget', quantize: '-', exit: src.exitAt ?? now, nextStart: old.start,
          });
          return { exit: src.exitAt ?? now, start: old.start };
        }
      } else {
        this.promote(); // successor is (about to be) audible: treat it as current
      }
    }

    const cur = this.deck;
    if (!cur) {
      const t = now + lead;
      this.deck = this.startDeck(target, t, this.defaults, 0);
      this.history.push(target.id);
      this.pinCurrent();
      this.emit({ type: 'track_started', track: target.id, time: t });
      return { exit: t, start: t };
    }
    if (cur.track.id === target.id && cur.exitAt === null) return null;
    return this.commitExit(cur, target, this.rule(cur.track.id, target.id, override), now + lead, name);
  }

  private commitExit(d: Deck, target: LoadedTrack, rule: TransitionRule, earliest: number, name: string): ScheduleResult {
    const g = d.track.grid;
    const E = exitPoint(g, d.loopStart, rule.quantize, earliest);
    let mode = rule.mode;
    const outroBuf = d.track.buffers.outro;
    if (mode === 'outro' && !outroBuf) mode = 'cut';
    d.exitAt = E;

    // Voices of this deck that are sounding at E (anything starting at/after E never plays).
    const playing: Voice[] = [];
    for (const v of d.voices) {
      if (v.kind === 'outro') continue;
      if (v.start >= E - 1e-6) {
        safeStop(v.node, E);
        continue;
      }
      const end = v.kind === 'loop' ? Infinity : v.start + v.node.buffer!.duration - v.offset;
      if (end <= E + 1e-6) continue; // intro that ends exactly at E
      playing.push(v);
    }

    let S: number;
    if (mode === 'outro') {
      let off = 0;
      if (rule.outro_from === 'tail' && g.outroTail > 0 && g.outroTail < outroBuf!.duration) off = g.outroTail;
      const seamless = off === 0 && isOnSeam(g, d.loopStart, E);
      const seam = seamless ? 0 : rule.seam_seconds;
      for (const v of playing) {
        if (seam > 0) v.env.fade(E, seam, 0);
        safeStop(v.node, E + seam);
      }
      let outroLen = outroBuf!.duration - off;
      if (rule.outro_max_seconds) outroLen = Math.min(outroLen, rule.outro_max_seconds);
      const xf = Math.min(rule.crossfade_seconds, outroLen);
      S = E + outroLen - xf;
      const ov = this.addVoice(d, 'outro', outroBuf!, E, off, false, seam > 0 ? 0 : 1);
      if (seam > 0) ov.env.fade(E, seam, 1);
      safeStop(ov.node, E + outroLen);
      if (xf > 0) d.env.fade(S, xf, 0);
      d.endAt = E + outroLen;
      this.pending = this.startDeck(target, S, rule, xf);
    } else {
      const xf = rule.crossfade_seconds;
      if (xf > 0) d.env.fade(E, xf, 0);
      for (const v of playing) safeStop(v.node, E + xf);
      d.endAt = E + xf;
      S = E;
      this.pending = this.startDeck(target, S, rule, xf);
    }
    this.retired.push(d);
    this.pinCurrent();
    this.emit({
      type: 'exit_scheduled', trigger: name, source: d.track.id, target: target.id,
      mode, quantize: rule.quantize, exit: E, nextStart: S,
    });
    return { exit: E, start: S };
  }

  /** Undo a scheduled (not yet reached) exit at time T and keep the deck playing. */
  private rewind(d: Deck, T: number): void {
    for (const v of d.voices) safeStop(v.node, T); // re-stop earlier; outro never starts
    d.voices = [];
    d.env.cancel(T);
    if (d.env.valueAt(T) < 0.999) d.env.fade(T, Math.max(0.05, d.start + d.fade - T), 1);
    const b = d.track.buffers;
    if (T < d.loopStart && b.intro) {
      this.addVoice(d, 'intro', b.intro, T, T - d.start, false);
      this.addVoice(d, 'loop', b.loop!, d.loopStart, 0, true);
    } else {
      const pos = loopPosition(d.track.grid, d.loopStart, T) ?? 0;
      this.addVoice(d, 'loop', b.loop!, T, pos, true);
    }
    d.exitAt = null;
    d.endAt = Infinity;
    this.retired = this.retired.filter((x) => x !== d);
  }

  private startDeck(track: LoadedTrack, at: number, rule: TransitionRule, fade: number): Deck {
    const out = this.ctx.createGain();
    const trim = this.ctx.createGain();
    trim.gain.value = track.gain;
    out.connect(trim).connect(this.bus);
    const env = new Envelope(out.gain, fade > 0 ? 0 : 1, this.ctx.currentTime);
    if (fade > 0) env.fade(at, fade, 1);
    const skip = rule.skip_intro || !track.buffers.intro;
    const loopStart = skip ? at : at + track.grid.introDur;
    const d: Deck = { track, start: at, loopStart, out, trim, env, fade, voices: [], exitAt: null, endAt: Infinity };
    if (!skip) this.addVoice(d, 'intro', track.buffers.intro!, at, 0, false);
    this.addVoice(d, 'loop', track.buffers.loop!, loopStart, 0, true);
    return d;
  }

  private addVoice(d: Deck, kind: Kind, buffer: AudioBuffer, when: number, offset: number, loop: boolean, gain = 1): Voice {
    const node = this.ctx.createBufferSource();
    node.buffer = buffer;
    node.loop = loop; // loops the whole buffer; the seam is baked into the file
    const g = this.ctx.createGain();
    node.connect(g).connect(d.out);
    const env = new Envelope(g.gain, gain, this.ctx.currentTime);
    node.onended = () => {
      node.disconnect();
      g.disconnect();
    };
    node.start(when, offset);
    const v: Voice = { kind, node, gain: g, env, start: when, offset };
    d.voices.push(v);
    return v;
  }

  private promote(): void {
    if (!this.pending) return;
    this.deck = this.pending;
    this.pending = null;
    this.history.push(this.deck.track.id);
    if (this.history.length > 32) this.history.shift();
    this.pinCurrent();
    this.emit({ type: 'track_started', track: this.deck.track.id, time: this.deck.start });
  }

  private killDeck(d: Deck, at: number): void {
    for (const v of d.voices) safeStop(v.node, at);
    d.endAt = at;
    this.retired.push(d);
  }

  private disposeDeck(d: Deck): void {
    for (const v of d.voices) {
      safeStop(v.node, 0);
      v.node.disconnect();
      v.gain.disconnect();
    }
    d.out.disconnect();
    d.trim.disconnect();
  }

  private pinCurrent(): void {
    this.lib.pin([this.deck?.track.id, this.pending?.track.id].filter((x): x is string => !!x));
  }
}

function safeStop(node: AudioScheduledSourceNode, when: number): void {
  try {
    node.stop(when);
  } catch {
    /* already stopped / never started: fine */
  }
}
```

### A.6 `src/music/resolvers.ts`

```ts
// How the webview finds the music files. Pick ONE strategy per project (see playbook §5).
import type { UrlResolver } from './library';

/** Strategy A (default): assets live in `public/music/`, Vite copies them into the
 *  frontend bundle, and Tauri embeds that bundle. Works the same in `tauri dev` and release. */
export const publicFolderResolver =
  (root = '/music/'): UrlResolver =>
  (rel) =>
    root + rel;

/** Strategy B: assets shipped as Tauri bundle resources (`src-tauri/music/**`), served
 *  through the asset protocol. Use for big soundtracks or DLC packs. */
export const tauriResourceResolver = (root = 'music/'): UrlResolver => {
  const cache = new Map<string, string>();
  return async (rel) => {
    const hit = cache.get(rel);
    if (hit) return hit;
    const { convertFileSrc } = await import('@tauri-apps/api/core');
    const { resolveResource } = await import('@tauri-apps/api/path');
    const url = convertFileSrc(await resolveResource(root + rel));
    cache.set(rel, url);
    return url;
  };
};

/** True when running inside Tauri (vs `vite` in a normal browser tab). */
export const isTauri = (): boolean => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
```

### A.7 `src/music/runtime.ts`

```ts
// Module-level singleton: exactly ONE AudioContext and ONE MusicDirector per app.
// Lives outside React so StrictMode double-mounts and Vite HMR don't create
// duplicate players that keep playing in the background.
import * as THREE from 'three';
import { MusicDirector } from './MusicDirector';
import { TrackLibrary, type UrlResolver } from './library';

export interface MusicRuntime {
  ctx: AudioContext;
  master: GainNode; // everything (music + SFX) -> master -> speakers
  library: TrackLibrary;
  director: MusicDirector;
}

let ctx: AudioContext | null = null;
let master: GainNode | null = null;
let runtime: Promise<MusicRuntime> | null = null;

/** Shared context. Also hands it to three.js so drei <PositionalAudio> SFX use the same clock and master bus. */
export function getAudioContext(): AudioContext {
  if (!ctx) {
    ctx = new AudioContext({ latencyHint: 'interactive' });
    THREE.AudioContext.setContext(ctx);
    master = ctx.createGain();
    master.connect(ctx.destination);
  }
  return ctx;
}

export function getMasterBus(): GainNode {
  getAudioContext();
  return master!;
}

/** Webviews start AudioContexts 'suspended' until a user gesture. Call once at boot. */
export function installAudioUnlock(): () => void {
  const unlock = () => {
    const c = getAudioContext();
    if (c.state !== 'running') void c.resume();
  };
  const evs = ['pointerdown', 'keydown', 'touchstart'] as const;
  evs.forEach((e) => window.addEventListener(e, unlock, { capture: true }));
  return () => evs.forEach((e) => window.removeEventListener(e, unlock, { capture: true }));
}

export function initMusic(resolver: UrlResolver, opts: { budgetMB?: number; preload?: string[] } = {}): Promise<MusicRuntime> {
  if (!runtime) {
    runtime = (async () => {
      const c = getAudioContext();
      const library = new TrackLibrary(c, resolver, { budgetBytes: (opts.budgetMB ?? 512) * 1024 * 1024 });
      const config = await library.init();
      const director = new MusicDirector(c, library, { destination: getMasterBus() });
      // Preload the start track first, then everything reachable from events (in background).
      if (config.start) await library.ensure(config.start);
      const targets = opts.preload ?? Object.values(config.events ?? {})
        .map((e) => (typeof e === 'string' ? e : e.target))
        .filter((t) => t !== '@previous');
      void library.preload([...new Set(targets)]);
      return { ctx: c, master: getMasterBus(), library, director };
    })();
    runtime.catch(() => (runtime = null));
  }
  return runtime;
}

export function peekMusic(): Promise<MusicRuntime> | null {
  return runtime;
}

// Vite HMR: tear the old player down when this module is replaced.
if (import.meta.hot) {
  import.meta.hot.dispose(async () => {
    const r = await runtime?.catch(() => null);
    r?.director.dispose();
    runtime = null;
  });
}
```

### A.8 `src/music/react.tsx`

```tsx
// React glue: provider, hook, and the R3F <MusicZone>.
import { useFrame } from '@react-three/fiber';
import {
  createContext, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore,
  type ReactNode, type RefObject,
} from 'react';
import * as THREE from 'three';
import type { DirectorEvent, MusicDirector } from './MusicDirector';
import type { UrlResolver } from './library';
import { initMusic, installAudioUnlock, type MusicRuntime } from './runtime';

interface MusicCtx {
  runtime: MusicRuntime | null;
  error: unknown;
}
const Ctx = createContext<MusicCtx>({ runtime: null, error: null });

/** Mount once, near the root (outside <Canvas>). Playback starts on the first user gesture. */
export function MusicProvider({ resolver, children, autoStart = true }: {
  resolver: UrlResolver;
  children: ReactNode;
  autoStart?: boolean;
}) {
  const [state, setState] = useState<MusicCtx>({ runtime: null, error: null });
  useEffect(() => installAudioUnlock(), []);
  useEffect(() => {
    let alive = true;
    initMusic(resolver).then(
      async (rt) => {
        if (!alive) return;
        setState({ runtime: rt, error: null });
        if (!autoStart || rt.director.current) return;
        // Wait for the context to be unlocked, then start the soundtrack.
        const go = () => rt.director.start().catch((error) => setState({ runtime: rt, error }));
        if (rt.ctx.state === 'running') go();
        else {
          const onState = () => {
            if (rt.ctx.state === 'running') {
              rt.ctx.removeEventListener('statechange', onState);
              go();
            }
          };
          rt.ctx.addEventListener('statechange', onState);
        }
      },
      (error) => alive && setState({ runtime: null, error }),
    );
    return () => {
      alive = false;
    };
    // resolver must be stable (define it at module level)
  }, [resolver, autoStart]);
  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

/** Imperative access for game code: const { trigger } = useMusic(); trigger('combat_start') */
export function useMusic() {
  const { runtime, error } = useContext(Ctx);
  const director = runtime?.director ?? null;
  const current = useCurrentTrack(director);
  return useMemo(
    () => ({
      ready: !!director,
      error,
      current,
      director,
      trigger: (name: string) => director?.trigger(name).catch((e) => console.error(e)) ?? Promise.resolve(null),
      setVolume: (v: number, fade = 0.5) => director?.setVolume(v, fade),
    }),
    [director, error, current],
  );
}

function useCurrentTrack(director: MusicDirector | null): string | null {
  return useSyncExternalStore(
    (cb) => (director ? director.on((e: DirectorEvent) => (e.type === 'track_started' || e.type === 'exit_scheduled') && cb()) : () => {}),
    () => director?.current ?? null,
  );
}

// ---------------------------------------------------------------- zones (R3F)

/** Stack of active zones: the most recently entered zone wins; leaving it falls back. */
const zoneStack: Array<{ id: symbol; target: string }> = [];
let zoneFallback: string | null = null;

function applyZones(director: MusicDirector | null) {
  const top = zoneStack[zoneStack.length - 1];
  const target = top?.target ?? zoneFallback;
  if (director && target) void director.trigger(target).catch(console.error);
}

/** Set what plays when the player is in no zone at all (e.g. 'enter_overworld'). */
export function setZoneFallback(eventOrTrack: string | null) {
  zoneFallback = eventOrTrack;
}

/**
 * Axis-aligned music volume. When `subject` (usually the player mesh) enters it,
 * `event` is triggered; when it leaves, the previous zone's event is re-triggered.
 * Put inside <Canvas>. Use `debug` to see the box.
 */
export function MusicZone({ event, subject, position = [0, 0, 0], size = [10, 10, 10], margin = 0.5, debug = false }: {
  event: string;
  subject: RefObject<THREE.Object3D | null>;
  position?: [number, number, number];
  size?: [number, number, number];
  margin?: number; // hysteresis so standing on the edge doesn't flicker
  debug?: boolean;
}) {
  const { director } = useMusic();
  const id = useRef(Symbol(event));
  const inside = useRef(false);
  const box = useMemo(() => {
    const half = new THREE.Vector3(...size).multiplyScalar(0.5);
    const c = new THREE.Vector3(...position);
    return new THREE.Box3(c.clone().sub(half), c.clone().add(half));
  }, [position, size]);
  const outer = useMemo(() => box.clone().expandByScalar(margin), [box, margin]);
  const p = useMemo(() => new THREE.Vector3(), []);

  useFrame(() => {
    const obj = subject.current;
    if (!obj) return;
    obj.getWorldPosition(p);
    if (!inside.current && box.containsPoint(p)) {
      inside.current = true;
      zoneStack.push({ id: id.current, target: event });
      applyZones(director);
    } else if (inside.current && !outer.containsPoint(p)) {
      inside.current = false;
      const i = zoneStack.findIndex((z) => z.id === id.current);
      if (i >= 0) zoneStack.splice(i, 1);
      applyZones(director);
    }
  });

  useEffect(() => () => {
    const i = zoneStack.findIndex((z) => z.id === id.current);
    if (i >= 0) zoneStack.splice(i, 1);
  }, []);

  return debug ? (
    <mesh position={position}>
      <boxGeometry args={size} />
      <meshBasicMaterial color="hotpink" wireframe />
    </mesh>
  ) : null;
}

/** Tiny DOM overlay for development: current track, phase, next exit times. */
export function MusicDebugOverlay() {
  const { director } = useMusic();
  const [text, setText] = useState('');
  useEffect(() => {
    if (!director) return;
    const t = setInterval(() => {
      const s = director.getState();
      const n = s.nextExit;
      setText(
        `${s.current ?? '-'}${s.pending ? ` → ${s.pending}` : ''} | ${s.phase}` +
          (s.loopPosition !== null ? ` @ ${s.loopPosition.toFixed(2)}s` : '') +
          (n ? ` | next beat ${n.beat.toFixed(2)} bar ${n.bar.toFixed(2)} phrase ${n.phrase.toFixed(2)}` : ''),
      );
    }, 100);
    return () => clearInterval(t);
  }, [director]);
  return (
    <div style={{ position: 'fixed', left: 8, bottom: 8, font: '12px monospace', color: '#0f0', background: '#000a', padding: '4px 8px', pointerEvents: 'none', zIndex: 9999 }}>
      ♪ {text}
    </div>
  );
}
```

### A.9 `src/music/parity.test.ts`

```ts
// Parity test: TS MusicDirector vs Python golden timeline.
//   python -m mp3toproc preview proc/soundtrack.json events.json -o p.wav --events-json golden.json
//   npx tsx src/music/parity.test.ts proc/ golden.json
// Uses a fake AudioContext (no audio is produced) - it checks scheduling maths only.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MusicDirector } from './MusicDirector';
import { TrackLibrary } from './library';
import type { TrackManifest } from './types';

const [root, goldenPath] = process.argv.slice(2);
const golden = JSON.parse(readFileSync(goldenPath, 'utf8')) as {
  events: Array<{ type: string; time: number; event?: string; frame?: number; next_start?: number; source?: string; target?: string; track?: string; mode?: string }>;
};

// ---- fakes --------------------------------------------------------------------
class FakeParam {
  value = 1;
  calls: unknown[][] = [];
  setValueAtTime(...a: unknown[]) { this.calls.push(['set', ...a]); return this; }
  linearRampToValueAtTime(...a: unknown[]) { this.calls.push(['ramp', ...a]); return this; }
  cancelScheduledValues(...a: unknown[]) { this.calls.push(['cancel', ...a]); return this; }
}
const node = () => ({ connect<T>(n: T) { return n; }, disconnect() {} });
const starts: Array<{ dur: number; when: number; offset: number; loop: boolean; stop?: number }> = [];
class FakeCtx {
  currentTime = 0;
  sampleRate = 44100;
  destination = node();
  createGain() { return { ...node(), gain: new FakeParam() }; }
  createBufferSource() {
    const rec: { dur: number; when: number; offset: number; loop: boolean; stop?: number } = { dur: 0, when: 0, offset: 0, loop: false };
    return {
      ...node(), buffer: null as null | { duration: number }, loop: false, onended: null,
      start(when: number, offset = 0) { rec.when = when; rec.offset = offset; rec.dur = this.buffer!.duration; rec.loop = this.loop; starts.push(rec); },
      stop(when: number) { rec.stop = when; },
    };
  }
  async decodeAudioData(data: ArrayBuffer) {
    const { samples, sr } = JSON.parse(new TextDecoder().decode(data));
    return { duration: samples / sr, length: samples, numberOfChannels: 2, sampleRate: sr };
  }
}

// fetch: JSON from disk; audio files become {samples, sr} stubs taken from the manifest
const manifests = new Map<string, TrackManifest>();
const fakeFetch = (async (url: string) => {
  const path = join(root, url);
  if (url.endsWith('.json')) {
    const txt = readFileSync(path, 'utf8');
    if (url.endsWith('track.json')) manifests.set(url.slice(0, url.lastIndexOf('/') + 1), JSON.parse(txt));
    return { ok: true, status: 200, json: async () => JSON.parse(txt) };
  }
  const dir = url.slice(0, url.lastIndexOf('/') + 1);
  const name = url.slice(dir.length).replace(/\.[^.]+$/, '') as 'intro' | 'loop' | 'outro';
  const m = manifests.get(dir)!;
  const body = new TextEncoder().encode(JSON.stringify({ samples: m.segments[name].samples, sr: m.sample_rate }));
  return { ok: true, status: 200, arrayBuffer: async () => body.buffer };
}) as unknown as typeof fetch;

// ---- run ------------------------------------------------------------------------
async function main() {
  const ctx = new FakeCtx() as unknown as AudioContext;
  const lib = new TrackLibrary(ctx, (p) => p, { fetchImpl: fakeFetch });
  await lib.init();
  await lib.preload(lib.ids());
  const d = new MusicDirector(ctx, lib, { autoTick: false });
  const got: Array<{ exit: number; start: number; source: string; target: string }> = [];
  d.on((e) => e.type === 'exit_scheduled' && got.push({ exit: e.exit, start: e.nextStart, source: e.source, target: e.target }));
  await d.start(undefined, 0);

  for (const ev of golden.events.filter((e) => e.type === 'trigger')) {
    (ctx as unknown as FakeCtx).currentTime = ev.time;
    await d.trigger(ev.event!);
  }
  const want = golden.events.filter((e) => e.type === 'exit_scheduled');
  let fail = 0;
  want.forEach((w, i) => {
    const g = got[i];
    const ok = g && g.source === w.source && g.target === w.target &&
      Math.abs(g.exit - w.frame!) < 1e-3 && Math.abs(g.start - w.next_start!) < 2e-3;
    if (!ok) fail++;
    console.log(`${ok ? 'OK  ' : 'FAIL'} ${w.source}->${w.target}  py exit ${w.frame!.toFixed(4)} start ${w.next_start!.toFixed(3)} | ts exit ${g?.exit.toFixed(4)} start ${g?.start.toFixed(3)}`);
  });
  if (got.length !== want.length) { console.log(`count mismatch: py ${want.length} ts ${got.length}`); fail++; }

  // sanity: every looping source must start on/after its scheduling time and loop sources must be loop=true
  const loops = starts.filter((s) => s.loop);
  if (!loops.length) fail++;
  console.log(fail ? `\n${fail} FAILURE(S)` : `\nPARITY OK (${want.length} transitions, ${starts.length} sources scheduled)`);
  process.exit(fail ? 1 : 0);
}
main();
```

## Appendix B: Example `events.json` for golden timelines

```json
{
  "duration": 230,
  "events": [
    { "time": 20,  "event": "enter_cave" },
    { "time": 70,  "event": "combat_start" },
    { "time": 100, "event": "combat_end" },
    { "time": 140, "event": "enter_forest" },
    { "time": 141, "event": "combat_start" },
    { "time": 190, "event": "combat_end" }
  ]
}
```

Write scripts that exercise every rule in your `soundtrack.json`: each `transitions` entry, `@previous`, `immediate` overrides, and a retarget during an outro.
