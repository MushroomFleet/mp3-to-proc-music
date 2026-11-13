# 🎵 MP3toPROC

**Turn finished songs into adaptive, procedural game soundtracks.**

MP3toPROC (*MP3 → Procedural*) is a Python + PowerShell toolkit for indie game developers and composers. If you have a folder full of finished tracks, it cuts each one into an **intro**, a **seamless loop** and an **outro**. A small runtime "music director" then moves between tracks in response to game events: entering a location, starting combat, spotting a boss.

```
Track 1 intro → Track 1 loop (repeats) → Track 1 outro ⟶ crossfade ⟶ Track 2 intro → Track 2 loop → …
                                ▲
                  game event / location trigger
              (waits for the next musical exit point)
```

---

## 🎯 What it's for

Linear songs don't suit games. A player can spend 40 seconds in an area or 40 minutes there. Middleware like FMOD and Wwise can play adaptive music, but only if the music was written and split into stems that way from the start.

MP3toPROC works backwards from music you've **already finished**:

- **Reuse your back catalogue.** Any MP3, WAV, OGG or FLAC you've made becomes a game-ready adaptive cue. You don't need to re-export stems or re-arrange anything.
- **Music that never runs out.** The main body of each song loops seamlessly for as long as the player stays in that state.
- **Changes that land on the beat.** When the game asks for a new track, playback waits for the next beat, bar, phrase or loop end. Then it plays the outro and crossfades into the next song's intro.
- **The game decides the order.** Transitions follow an editable graph (`soundtrack.json`), so locations, events, combat or story beats can send the music anywhere, including back to whatever was playing before.
- **Engine-agnostic.** The output is plain audio files plus JSON manifests. A sample-accurate Python reference player is included, and the README explains how to port it to Unity or Godot.

---

## ✨ Features

| Half | What it does |
|---|---|
| **Converter** (offline) | Finds the tempo, bar grid, sections, intro and outro, and the best seamless loop in each song. It writes `intro` / `loop` / `outro` audio, a `track.json` manifest and a `review.png`. |
| **MusicDirector** (runtime) | An event-driven state machine. It waits for a quantized exit point, plays the outro and equal-power crossfades into the next track. If the target changes while a transition is pending, it handles that too. |
| **Batch tooling** | Converts whole folders in parallel. A PowerShell wrapper handles setup, conversion, preview renders and live auditioning. |
| **Preview & audition** | Renders a scripted playthrough to WAV, with an Audacity label track for checking the timing. You can also audition live by typing event names. |

---

## 🚀 Quick start

### Windows (PowerShell)

```powershell
git clone https://github.com/MushroomFleet/mp3-to-proc-music
cd mp3-to-proc-music
.\MP3toPROC.ps1 -Setup                                    # one-time: makes .venv, installs deps
.\MP3toPROC.ps1 -InputDir "D:\Music\Mine" -OutputDir "D:\Game\Audio\Music"
# check the review.png files, edit soundtrack.json, then:
.\MP3toPROC.ps1 -Preview -OutputDir "D:\Game\Audio\Music" -Script .\examples\events.example.json
.\MP3toPROC.ps1 -Play    -OutputDir "D:\Game\Audio\Music"  # live: type event names, hear transitions
```

Options: `-Format ogg|wav|flac` (default ogg), `-Jobs 4`, `-Crossfade 4`, `-PhraseBars 4`, `-Overrides file.json`, `-SkipExisting`.

### Any OS (Python 3.9+)

```bash
pip install -r requirements.txt            # + sounddevice for `play`
python -m mp3toproc batch   songs/ -o proc/ -j 4
python -m mp3toproc convert song.mp3 -o proc/ --bpm 128 --loop-start-bar 16 --loop-end-bar 48
python -m mp3toproc graph   proc/          # create/update soundtrack.json (keeps your edits)
python -m mp3toproc preview proc/soundtrack.json examples/events.example.json -o preview.wav
python -m mp3toproc play    proc/soundtrack.json
```

Add `--also-format flac` (`-AlsoFormat flac` in PowerShell) to ship fallback copies for webviews that can't decode Ogg, such as macOS before 15.4. Add `--events-json golden.json` to `preview` to export the timeline that engine ports are tested against.

`preview` also writes `preview_labels.txt`. In Audacity, use *File → Import → Labels* to see every trigger, exit and track start on the timeline.

---

## 📁 Output layout

```
proc/
  soundtrack.json          ← transition graph (you edit this)
  forest_dawn/
    intro.ogg              song start → loop start
    loop.ogg               seamless loop body (seam crossfade baked in; loops in ANY player)
    outro.ogg              loop end → song end
    track.json             manifest: bpm, sample counts, exit-point grids, analysis scores
    review.png             waveform with intro/loop/outro shaded + section boundaries
```

The three files join back into the original song with sample accuracy: `intro + loop + outro = original`.

---

## 🔬 How the loop is found

1. **Beat grid.** The beat tracker runs first. The grid is then fitted to a constant tempo, phase-corrected toward the kick (so it doesn't lock onto off-beat hi-hats), extended across quiet intros and outros, and aligned to the real attack onsets.
2. **Downbeats.** The beat phase with the strongest low-end hits and chord changes is taken as the downbeat.
3. **Sections.** Bar-level chroma, MFCC and energy feed a self-similarity matrix. Checkerboard novelty on that matrix gives the section boundaries. Energy then marks where the intro ends and where the outro begins.
4. **Loop search.** Every phrase-friendly pair of bars (A, B) around those boundaries is scored. The score combines how much *the music after B* sounds like *the music after A* and how much *the music before B* sounds like *the music before A*. There are bonuses for long loops and for 8- or 16-bar lengths.
5. **Sample-level refinement.** A is snapped to the nearest transient. B is slid ±25 ms to the point of best waveform correlation. A short crossfade is then baked into the head of `loop`, so the wrap B→A is the song's own continuation.

Each track gets a **seam confidence**: `high` above 0.85, `medium`, or `low`. For anything not `high`, listen to the loop and override the bars if needed.

---

## 🗺️ soundtrack.json: the transition graph

```jsonc
{
  "sample_rate": 44100,
  "start": "forest_dawn",
  "tracks": {
    "forest_dawn": "forest_dawn/track.json",
    "cave_echoes": { "manifest": "cave_echoes/track.json", "gain": 0.9 }
  },
  "defaults": {
    "mode": "outro",            // outro | cut
    "quantize": "phrase",       // immediate | beat | bar | phrase | loop_end
    "crossfade_seconds": 4.0,   // outro tail overlaps next intro by this much
    "outro_max_seconds": 12,    // trim long outros (fades out)
    "outro_from": "start",      // start | tail (jump straight to the song's real ending)
    "skip_intro": false,        // enter the target directly at its loop
    "seam_seconds": 0.12,       // micro-crossfade when leaving mid-loop
    "lead_seconds": 0.05        // scheduling headroom
  },
  "transitions": [              // later rules override earlier; "*" = any
    { "from": "*", "to": "battle_rush", "mode": "cut", "quantize": "beat",
      "crossfade_seconds": 1.0, "skip_intro": true },
    { "from": "battle_rush", "to": "*", "quantize": "bar", "outro_max_seconds": 8 }
  ],
  "events": {                   // what your game code calls
    "enter_cave":   "cave_echoes",
    "combat_start": "battle_rush",
    "combat_end":   "@previous",          // return to whatever played before
    "boss_spotted": { "target": "battle_rush", "quantize": "immediate", "crossfade_seconds": 0.3 }
  }
}
```

**Modes**
- `outro`: wait for the exit point, play the outro, then start the next intro `crossfade_seconds` before the outro ends.
- `cut`: wait for the exit point, then crossfade straight from the current music into the next track. Use this for combat, danger and other urgent changes.

**Behaviour while a change is pending.** If a new trigger arrives before the next track can be heard (for example, the player walks into the cave and combat starts a second later), the pending change is cancelled and recalculated toward the new target. Triggering the track that is already playing does nothing.

---

## 🎮 Using it in your game

**Engine playbooks** (hand these to your developers or coding agent):
- **Tauri + React + three.js / R3F:** [`mp3toproc-tauri-react-PLAYBOOK.md`](mp3toproc-tauri-react-PLAYBOOK.md), with a tested TypeScript runtime in [`runtimes/tauri-react/`](runtimes/tauri-react/)
- Unreal / Unity: planned


`mp3toproc/director.py` is the reference implementation. All of its timing is sample-accurate and engine-agnostic:

```python
from mp3toproc.director import MusicDirector

d = MusicDirector("proc/soundtrack.json", on_event=print)
d.trigger("enter_cave")          # from game code, any time
block = d.render(1024)           # from the audio callback
```

To port it to an engine, translate `_exit_point` and `_commit_exit` (about 80 lines):

| Concept | Unity | Godot 4 |
|---|---|---|
| sample clock | `AudioSettings.dspTime` | `AudioServer.get_time_since_last_mix()` + mix time |
| loop | `AudioSource.loop = true` on `loop.ogg` | `AudioStreamOggVorbis.loop = true` |
| exit at frame E | `PlayScheduled(E)` / `SetScheduledEndTime(E)` | timer to E, `play()` outro |
| crossfade | two AudioSources, equal-power volume ramps | two players, `Tween` on `volume_db` |
| exit grids | `track.json → segments.loop.phrase_offsets` (samples from loop start) | same |

In practice: `loop position = (now − loop_start) mod loop_samples`. Take the next value in `phrase_offsets` (or `bar_offsets` / `beat_offsets`) that comes after that position. That point is E. Schedule the outro at E, and the next intro at `E + outro_len − crossfade`.

---

## 💡 Tips for real songs

- **Wrong tempo (half or double time):** pass `--bpm 174`. In batch, add `{"Song.mp3": {"bpm": 174}}` to an overrides file.
- **Bad loop choice:** look at `review.png`, then set `--loop-start-bar` and `--loop-end-bar`. Bars count from the first downbeat. Section boundaries are listed in `track.json → analysis.section_bars`.
- **Long reverb tails at the loop end:** the baked crossfade covers short tails. For big ambient washes, try `--loop-xfade-ms 150`.
- **Non-4/4 music:** `--beats-per-bar 3`.
- **Limitations:** the converter assumes a mostly constant tempo, which suits almost all DAW-made music. It needs a song body that repeats somewhere. Through-composed pieces with no repeats will get a `low` seam score.

---

## 🧪 Tests

```bash
python tools/make_test_songs.py test_songs          # synthetic structured songs
python -m mp3toproc batch test_songs -o proc_out
cp examples/soundtrack.example.json proc_out/soundtrack.json
python tests/test_director.py proc_out/soundtrack.json
```

---

## 🧱 Project structure

```
mp3toproc/
  analyze.py     tempo, downbeats, sections, loop search, sample-accurate refinement
  export.py      slicing, loop-seam baking, manifests, review plots
  director.py    runtime MusicDirector + offline preview renderer
  cli.py         convert / batch / graph / preview / play
runtimes/
  tauri-react/   TypeScript Web Audio runtime + parity test
MP3toPROC.ps1    Windows batch wrapper (setup, convert, preview, play)
mp3toproc-tauri-react-PLAYBOOK.md   game-side integration guide (Tauri/React/R3F)
examples/        sample soundtrack graph + scripted event timeline
tests/           director smoke tests
tools/           synthetic test-song generator
```

---

## 📚 Citation

### Academic Citation

If you use this codebase in your research or project, please cite:

```bibtex
@software{mp3_to_proc_music,
  title = {MP3toPROC: Converting finished songs into adaptive, procedural game soundtracks},
  author = {Drift Johnson},
  year = {2026},
  url = {https://github.com/MushroomFleet/mp3-to-proc-music},
  version = {1.0.0}
}
```

### Donate:

[![Ko-Fi](https://cdn.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/driftjohnson)
