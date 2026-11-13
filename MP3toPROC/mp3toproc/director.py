"""MusicDirector - the runtime half of MP3toPROC.

Engine-agnostic, sample-accurate reference implementation of the adaptive
playback logic:

    intro -> loop (repeats) -> [trigger] -> wait for musical exit point
          -> outro  [crossfade]  next intro -> next loop ...

The director only needs `render(n_frames)` to be called from an audio callback
(or offline). `trigger(name)` can be called from game code at any time with an
event name ("enter_cave") or a track id ("battle_rush").

Porting to Unity / Godot / FMOD-less engines: the logic in `_commit_exit` is
the whole trick - compute the exit frame E from the loop grid, then schedule
the outro at E and the next intro at E + outro_len - crossfade, with equal-power
fades. In Unity that maps to AudioSource.PlayScheduled + SetScheduledEndTime on
dspTime; in Godot to AudioStreamPlayer with a position-tracked mixer.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import soundfile as sf

QUANTIZE_MODES = ("immediate", "beat", "bar", "phrase", "loop_end")
MODES = ("outro", "cut")


# --------------------------------------------------------------------------- data

class Track:
    def __init__(self, manifest_path: str, engine_sr: int, gain: float = 1.0):
        with open(manifest_path) as f:
            m = json.load(f)
        self.manifest = m
        self.id = m["id"]
        self.gain = gain
        base = os.path.dirname(manifest_path)
        src_sr = m["sample_rate"]
        ratio = engine_sr / src_sr
        self.bpm = m["bpm"]
        self.beat_len = 60.0 / self.bpm * engine_sr
        self.bar_len = self.beat_len * m.get("beats_per_bar", 4)
        self.buffers: Dict[str, np.ndarray] = {}
        for name in ("intro", "loop", "outro"):
            seg = m["segments"][name]
            if seg.get("file"):
                x, sr = sf.read(os.path.join(base, seg["file"]), dtype="float32", always_2d=True)
                if sr != engine_sr:
                    import librosa
                    x = librosa.resample(x.T, orig_sr=sr, target_sr=engine_sr).T.astype(np.float32)
                if x.shape[1] == 1:
                    x = np.repeat(x, 2, axis=1)
                self.buffers[name] = x[:, :2] * gain
            else:
                self.buffers[name] = np.zeros((0, 2), np.float32)
        loop = m["segments"]["loop"]
        sc = lambda lst: [int(round(v * ratio)) for v in lst]
        self.loop_len = len(self.buffers["loop"])
        self.offsets = {
            "beat": sc(loop.get("beat_offsets", [0])),
            "bar": sc(loop.get("bar_offsets", [0])),
            "phrase": sc(loop.get("phrase_offsets", [0])),
            "loop_end": [0],
        }
        self.outro_tail = int(round(m["segments"]["outro"].get("tail_offset", 0) * ratio))
        self.phrase_len = (self.offsets["phrase"][1] - self.offsets["phrase"][0]
                           if len(self.offsets["phrase"]) > 1 else self.bar_len * 4)

    def length(self, seg):
        return len(self.buffers[seg])


@dataclass(eq=False)
class Voice:
    buf: np.ndarray
    start: int                    # absolute engine frame where buf[0] plays
    end: int                      # absolute frame where the voice stops
    owner: "Deck"
    kind: str                     # intro / loop / outro
    fade_in: Optional[tuple] = None   # (abs_start, length)
    fade_out: Optional[tuple] = None


@dataclass(eq=False)
class Deck:
    track: Track
    start: int                    # abs frame the deck becomes audible
    loop_start: int               # abs frame of first loop iteration
    exit_at: Optional[int] = None
    next_iter: int = 0            # abs frame of next loop iteration to schedule
    voices: List[Voice] = field(default_factory=list)
    first_fade: int = 0           # fade-in for the first loop pass when intro is skipped
    fade: int = 0                 # fade-in length the deck was started with


# --------------------------------------------------------------------------- director

class MusicDirector:
    def __init__(self, soundtrack_path: str, sample_rate: Optional[int] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        with open(soundtrack_path) as f:
            self.cfg = json.load(f)
        base = os.path.dirname(os.path.abspath(soundtrack_path))
        self.sr = sample_rate or self.cfg.get("sample_rate", 44100)
        self.tracks: Dict[str, Track] = {}
        for tid, spec in self.cfg["tracks"].items():
            path, gain = (spec, 1.0) if isinstance(spec, str) else (spec["manifest"], spec.get("gain", 1.0))
            self.tracks[tid] = Track(os.path.join(base, path), self.sr, gain)
        self.defaults = {"mode": "outro", "quantize": "phrase", "crossfade_seconds": 4.0,
                         "outro_max_seconds": None, "outro_from": "start", "skip_intro": False, "seam_seconds": 0.12,
                         "lead_seconds": 0.05}
        self.defaults.update(self.cfg.get("defaults", {}))
        self.rules = self.cfg.get("transitions", [])
        self.events = self.cfg.get("events", {})
        self.on_event = on_event or (lambda kind, info: None)

        self.now = 0
        self.voices: List[Voice] = []
        self.deck: Optional[Deck] = None      # audible / current
        self.pending: Optional[Deck] = None   # scheduled, not yet audible
        self.history: List[str] = []
        self.lookahead = int(0.5 * self.sr)
        start = self.cfg.get("start")
        if start:
            self.deck = self._start_deck(self.tracks[start], 0, {"skip_intro": False})
            self._emit("track_started", track=start, frame=0)

    # ---------------------------------------------------------------- public API
    def trigger(self, name: str) -> Optional[dict]:
        """Request a change. `name` is an event (from soundtrack.json) or a track id."""
        target = self.events.get(name, name)
        override = {}
        if isinstance(target, dict):
            override = {k: v for k, v in target.items() if k != "target"}
            target = target["target"]
        if target == "@previous":
            cur = self._current_id()
            prev = [t for t in reversed(self.history) if t != cur]
            if not prev:
                return None
            target = prev[0]
        if target not in self.tracks:
            raise KeyError(f"Unknown event or track: {name!r}")
        return self._request(self.tracks[target], override, name)

    def render(self, n: int) -> np.ndarray:
        self._update(self.now + n)
        out = np.zeros((n, 2), np.float32)
        t0, t1 = self.now, self.now + n
        for v in self.voices:
            s, e = max(v.start, t0), min(v.end, t1, v.start + len(v.buf))
            if e <= s:
                continue
            frames = np.arange(s, e)
            g = np.ones(e - s, np.float32)
            if v.fade_in:
                fs, fl = v.fade_in
                g *= np.sin(np.clip((frames - fs) / max(1, fl), 0, 1) * math.pi / 2)
            if v.fade_out:
                fs, fl = v.fade_out
                g *= np.cos(np.clip((frames - fs) / max(1, fl), 0, 1) * math.pi / 2)
            out[s - t0:e - t0] += v.buf[s - v.start:e - v.start] * g[:, None]
        self.now = t1
        self.voices = [v for v in self.voices if v.end > self.now and v.start + len(v.buf) > self.now]
        if self.pending and self.now >= self.pending.start:
            self.deck, self.pending = self.pending, None
            self.history.append(self.deck.track.id)
            self._emit("track_started", track=self.deck.track.id, frame=self.deck.start)
        return out

    def current_track(self) -> Optional[str]:
        return self._current_id()

    # ---------------------------------------------------------------- internals
    def _emit(self, kind, **info):
        info.setdefault("time", round(info.get("frame", self.now) / self.sr, 3))
        self.on_event(kind, info)

    def _current_id(self):
        d = self.pending or self.deck
        return d.track.id if d else None

    def _rule(self, src: Optional[str], dst: str, override: dict) -> dict:
        r = dict(self.defaults)
        for rule in self.rules:  # later rules win; specific beats wildcard by order
            if rule.get("from", "*") in ("*", src) and rule.get("to", "*") in ("*", dst):
                r.update({k: v for k, v in rule.items() if k not in ("from", "to")})
        r.update(override)
        return r

    def _start_deck(self, track: Track, at: int, rule: dict, fade: int = 0) -> Deck:
        skip = rule.get("skip_intro") or track.length("intro") == 0
        loop_start = at if skip else at + track.length("intro")
        deck = Deck(track=track, start=at, loop_start=loop_start, next_iter=loop_start, fade=fade)
        if not skip:
            self._add(Voice(track.buffers["intro"], at, at + track.length("intro"), deck, "intro",
                            fade_in=(at, fade) if fade else None))
        elif fade:
            deck.first_fade = fade  # applied to first loop iteration
        if self.deck is None:
            self.history.append(track.id)
        return deck

    def _add(self, v: Voice):
        v.owner.voices.append(v)
        self.voices.append(v)

    def _update(self, horizon: int):
        for d in (self.deck, self.pending):
            if d is None or d.track.loop_len == 0:
                continue
            while d.next_iter < horizon + self.lookahead and (d.exit_at is None or d.next_iter < d.exit_at):
                fade = d.first_fade if d.next_iter == d.loop_start else 0
                end = d.next_iter + d.track.loop_len
                self._add(Voice(d.track.buffers["loop"], d.next_iter, end, d, "loop",
                                fade_in=(d.next_iter, fade) if fade else None))
                d.next_iter = end

    def _exit_point(self, deck: Deck, quantize: str, earliest: int) -> int:
        """First musically valid frame >= earliest to leave `deck`."""
        t = deck.track
        if quantize == "immediate":
            return earliest
        if earliest < deck.loop_start:
            # Still in the intro: count the grid backwards from the loop start.
            if quantize == "loop_end" or t.loop_len == 0:
                return deck.loop_start
            period = {"beat": t.beat_len, "bar": t.bar_len, "phrase": t.phrase_len}[quantize]
            k = math.floor((deck.loop_start - earliest) / period)
            return int(round(deck.loop_start - k * period))
        L = t.loop_len
        it, pos = divmod(earliest - deck.loop_start, L)
        offs = t.offsets[quantize] + [L]
        nxt = next(o for o in offs if o >= pos)
        return deck.loop_start + it * L + nxt

    def _request(self, target: Track, override: dict, name: str):
        lead = int(self.defaults["lead_seconds"] * self.sr)
        # A transition is already scheduled but its next track isn't audible yet: retarget it.
        if self.pending is not None and self.pending.start > self.now + lead:
            if self.pending.track is target:
                return None
            src = self.deck
            old = self.pending
            for v in old.voices:
                if v in self.voices:
                    self.voices.remove(v)
            self.pending = None
            if src.exit_at is not None and src.exit_at > self.now + lead:
                # the exit hasn't happened yet: undo it and recompute from scratch
                self._rewind_exit(src)
            else:
                # the outro is already playing: keep it, just swap what comes next
                rule = self._rule(src.track.id, target.id, override)
                self.pending = self._start_deck(target, old.start, rule, fade=old.fade)
                self._emit("exit_scheduled", trigger=name, source=src.track.id, target=target.id,
                           mode="retarget", quantize="-", frame=src.exit_at,
                           next_start=round(old.start / self.sr, 3))
                return {"exit": src.exit_at, "start": old.start}
        if self.pending is not None:  # successor already (about to be) audible: promote it
            self.deck, self.pending = self.pending, None
            self.history.append(self.deck.track.id)
            self._emit("track_started", track=self.deck.track.id, frame=self.deck.start)
        cur = self.deck
        if cur is None:
            self.deck = self._start_deck(target, self.now, {})
            self._emit("track_started", track=target.id, frame=self.now)
            return {"exit": self.now, "start": self.now}
        if cur.track is target and cur.exit_at is None:
            return None
        rule = self._rule(cur.track.id, target.id, override)
        return self._commit_exit(cur, target, rule, self.now + lead, name)

    def _rewind_exit(self, deck: Deck):
        """Remove outro/fades scheduled by a cancelled transition and resume looping."""
        for v in list(deck.voices):
            if v.kind == "outro" and v in self.voices:
                self.voices.remove(v)
                deck.voices.remove(v)
        for v in deck.voices:
            if v.kind in ("intro", "loop"):
                v.end = v.start + len(v.buf)
                v.fade_out = None
        last_loop = max((v.start + len(v.buf) for v in deck.voices if v.kind == "loop"), default=deck.loop_start)
        deck.next_iter = max(last_loop, deck.loop_start)
        deck.exit_at = None

    def _commit_exit(self, deck: Deck, target: Track, rule: dict, earliest: int, name: str):
        sr = self.sr
        q = rule["quantize"]
        if q not in QUANTIZE_MODES:
            raise ValueError(f"quantize must be one of {QUANTIZE_MODES}")
        E = self._exit_point(deck, q, earliest)
        xf = int(rule["crossfade_seconds"] * sr)
        seam = int(rule["seam_seconds"] * sr)
        mode = rule["mode"]
        t = deck.track
        if mode == "outro" and t.length("outro") == 0:
            mode = "cut"
        deck.exit_at = E

        # drop voices of this deck that would start at/after E
        for v in list(deck.voices):
            if v.start >= E and v.kind in ("intro", "loop"):
                if v in self.voices:
                    self.voices.remove(v)
                deck.voices.remove(v)
        playing = [v for v in deck.voices if v.kind in ("intro", "loop") and v.start < E < v.start + len(v.buf)]
        on_seam = not playing  # E falls exactly on a segment boundary

        if mode == "outro":
            for v in playing:
                v.end = E + seam
                v.fade_out = (E, seam)
            obuf = t.buffers["outro"]
            if rule.get("outro_from") == "tail" and 0 < t.outro_tail < len(obuf):
                obuf, on_seam = obuf[t.outro_tail:], False   # jump straight to the real ending
            outro_len = len(obuf)
            cap = rule.get("outro_max_seconds")
            if cap:
                outro_len = min(outro_len, int(cap * sr))
            xf = min(xf, outro_len)
            S = E + outro_len - xf
            self._add(Voice(obuf, E, E + outro_len, deck, "outro",
                            fade_in=None if on_seam else (E, seam),
                            fade_out=(S, xf) if xf else None))
        else:  # cut: crossfade straight from wherever we are into the next track
            for v in playing:
                v.end = E + xf
                v.fade_out = (E, xf)
            S = E
        nd = self._start_deck(target, S, rule, fade=xf)
        self.pending = nd
        self._emit("exit_scheduled", trigger=name, source=t.id, target=target.id, mode=mode,
                   quantize=q, frame=E, next_start=round(S / sr, 3))
        return {"exit": E, "start": S}


# --------------------------------------------------------------------------- offline preview

def render_preview(soundtrack: str, script: str, out_wav: str, labels_txt: Optional[str] = None,
                   block: int = 1024, log=print, events_json: Optional[str] = None) -> List[tuple]:
    """Render a scripted play-through to a WAV (and an Audacity label file)."""
    with open(script) as f:
        sc = json.load(f)
    labels: List[tuple] = []
    records: List[dict] = []
    with open(soundtrack) as f:
        sr_ = json.load(f).get("sample_rate", 44100)

    def on_event(kind, info):
        rec = {"type": kind}
        rec.update({k: (round(v / sr_, 6) if k == "frame" else v) for k, v in info.items()})
        records.append(rec)
        line = f"{info['time']:8.2f}s  {kind:15s} " + " ".join(
            f"{k}={v}" for k, v in info.items() if k not in ("time", "frame"))
        log(line)
        label = info.get("track") if kind == "track_started" else f"exit {info.get('source')}->{info.get('target')}"
        labels.append((info["time"], label))

    d = MusicDirector(soundtrack, on_event=on_event)
    events = sorted(sc["events"], key=lambda e: e["time"])
    dur = sc.get("duration") or (events[-1]["time"] + 60 if events else 120)
    total = int(dur * d.sr)
    chunks = []
    i = 0
    while d.now < total:
        while i < len(events) and events[i]["time"] * d.sr <= d.now:
            log(f"{d.now / d.sr:8.2f}s  >> trigger      {events[i]['event']}")
            records.append({"type": "trigger", "event": events[i]["event"], "time": round(d.now / d.sr, 6)})
            labels.append((round(d.now / d.sr, 3), "trigger " + events[i]["event"]))
            d.trigger(events[i]["event"])
            i += 1
        chunks.append(d.render(min(block, total - d.now)))
    audio = np.concatenate(chunks)
    peak = np.max(np.abs(audio)) or 1.0
    if peak > 1.0:
        audio /= peak
    sf.write(out_wav, audio, d.sr, subtype="PCM_16")
    if events_json:
        with open(events_json, "w") as f:
            json.dump({"sample_rate": d.sr, "block": block, "events": records}, f, indent=2)
    if labels_txt:
        with open(labels_txt, "w") as f:
            for t, lab in labels:
                f.write(f"{t:.3f}\t{t:.3f}\t{lab}\n")
    return labels
