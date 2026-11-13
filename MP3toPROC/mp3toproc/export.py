"""Convert one song into a procedural track package:

    <out>/<track_id>/
        intro.<fmt>    song start -> loop start
        loop.<fmt>     seamless loop body (crossfade baked into its head)
        outro.<fmt>    loop end -> song end
        track.json     manifest the runtime reads
        review.png     visual check of the cut points
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import numpy as np
import librosa
import soundfile as sf

from .analyze import analyze

MANIFEST_VERSION = 1


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(name))[0]).strip("_")
    return s.lower() or "track"


def bake_loop(y: np.ndarray, a: int, b: int, xfade: int) -> np.ndarray:
    """Return y[:, a:b] with its first `xfade` samples blended with y[:, b:b+xfade].

    When the loop wraps, playback goes  y[b-1] -> loop[0]. Because loop[0] starts as
    (mostly) y[b], the wrap is the song's own continuation, which then fades into
    y[a]. No click, no gap, works with any player that can loop a buffer.
    """
    L = y[:, a:b].copy()
    n = min(xfade, L.shape[1] // 4, y.shape[1] - b)
    if n > 0:
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        fade_in = np.sin(t * np.pi / 2) ** 2      # smooth, sums to 1 with fade_out
        fade_out = 1.0 - fade_in
        L[:, :n] = y[:, a:a + n] * fade_in + y[:, b:b + n] * fade_out
    return L


def _edge_fade(x: np.ndarray, sr: int, head_ms=0.0, tail_ms=0.0):
    x = x.copy()
    h, t = int(sr * head_ms / 1000), int(sr * tail_ms / 1000)
    if h > 0 and x.shape[1] > h:
        x[:, :h] *= np.linspace(0, 1, h)
    if t > 0 and x.shape[1] > t:
        x[:, -t:] *= np.linspace(1, 0, t)
    return x


def write_audio(path, x, sr, fmt):
    data = x.T.astype(np.float32)
    if fmt == "ogg":
        # libsndfile's Vorbis encoder can crash on large single writes; stream it.
        with sf.SoundFile(path, "w", sr, data.shape[1], format="OGG", subtype="VORBIS") as f:
            for i in range(0, len(data), 16384):
                f.write(data[i:i + 16384])
    elif fmt == "flac":
        sf.write(path, data, sr, format="FLAC", subtype="PCM_16")
    else:
        sf.write(path, data, sr, subtype="PCM_16")


def convert(src: str, out_root: str, *, fmt: str = "wav", track_id: Optional[str] = None,
            bpm: Optional[float] = None, beats_per_bar: int = 4,
            loop_start_bar: Optional[int] = None, loop_end_bar: Optional[int] = None,
            loop_xfade_ms: float = 40.0, exit_every_bars: int = 4,
            crossfade_seconds: float = 4.0, plot: bool = True, log=print,
            also_formats=()) -> dict:
    track_id = track_id or slugify(src)
    out_dir = os.path.join(out_root, track_id)
    os.makedirs(out_dir, exist_ok=True)

    y, sr = librosa.load(src, sr=None, mono=False)
    if y.ndim == 1:
        y = np.stack([y, y])
    y = y.astype(np.float32)

    log(f"[{track_id}] analysing {os.path.basename(src)} ({y.shape[1] / sr:.1f}s, {sr} Hz)")
    an = analyze(y, sr, bpm_hint=bpm, beats_per_bar=beats_per_bar,
                 loop_start_bar=loop_start_bar, loop_end_bar=loop_end_bar)
    a, b = an.loop_start_sample, an.loop_end_sample

    intro = _edge_fade(y[:, :a], sr, head_ms=2)
    loop = bake_loop(y, a, b, int(sr * loop_xfade_ms / 1000))
    outro = _edge_fade(y[:, b:], sr, tail_ms=20)

    ext = {"wav": "wav", "ogg": "ogg", "flac": "flac"}[fmt]
    files = {}
    for name, x in (("intro", intro), ("loop", loop), ("outro", outro)):
        fn = f"{name}.{ext}"
        if x.shape[1] > 0:
            write_audio(os.path.join(out_dir, fn), x, sr, fmt)
            for extra in also_formats:  # same audio, other codecs (runtime fallback)
                if extra != fmt:
                    write_audio(os.path.join(out_dir, f"{name}.{extra}"), x, sr, extra)
            files[name] = (fn, x.shape[1])
        else:
            files[name] = (None, 0)

    # Musical exit points inside the loop (relative samples), aligned to the
    # song's phrase grid (counted from the detected intro boundary).
    bars = np.array(an.bar_samples)
    half_beat = int(30.0 / an.bpm * sr)
    snap0 = lambda o: 0 if o < half_beat else o   # loop start was transient-snapped
    in_loop = [(i, snap0(max(0, int(bars[i] - a))))
               for i in range(an.loop_start_bar, min(an.loop_end_bar, len(bars)))]
    bar_offsets = [o for _, o in in_loop]
    phrase_offsets = [o for i, o in in_loop if (i - an.intro_end_bar) % exit_every_bars == 0]
    if not phrase_offsets or phrase_offsets[0] != 0:
        phrase_offsets = [0] + phrase_offsets
    beats = np.array(an.beat_samples)
    beat_offsets = sorted({snap0(max(0, int(s - a))) for s in beats if a - half_beat <= s < b - half_beat})

    def seg(name, extra=None):
        fn, n = files[name]
        d = {"file": fn, "samples": int(n), "seconds": round(n / sr, 4)}
        if extra:
            d.update(extra)
        return d

    manifest = {
        "mp3toproc_version": MANIFEST_VERSION,
        "id": track_id,
        "source": os.path.basename(src),
        "sample_rate": sr,
        "channels": int(y.shape[0]),
        "bpm": an.bpm,
        "beats_per_bar": beats_per_bar,
        "segments": {
            "intro": seg("intro"),
            "loop": seg("loop", {
                "bars": len(bar_offsets),
                "bar_offsets": bar_offsets,
                "phrase_offsets": phrase_offsets,
                "beat_offsets": beat_offsets,
            }),
            "outro": seg("outro", {
                # where the song's real ending section starts inside outro (samples)
                "tail_offset": int(max(0, (an.bar_samples[an.outro_start_bar] if an.outro_start_bar < len(an.bar_samples) else b) - b)),
            }),
        },
        "transition_defaults": {
            "crossfade_seconds": crossfade_seconds,
            "quantize": "phrase",
        },
        "analysis": {
            "loop_source_seconds": [round(a / sr, 4), round(b / sr, 4)],
            "loop_bars": [an.loop_start_bar, an.loop_end_bar],
            "intro_end_bar": an.intro_end_bar,
            "outro_start_bar": an.outro_start_bar,
            "section_bars": an.section_bars,
            "loop_score": an.loop_score,
            "forward_similarity": an.fwd_similarity,
            "backward_similarity": an.back_similarity,
            "confidence": an.confidence,
            "warnings": an.warnings,
        },
    }
    with open(os.path.join(out_dir, "track.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    if plot:
        try:
            _plot(os.path.join(out_dir, "review.png"), y, sr, an, track_id)
        except Exception as e:  # plotting is a nicety, never fatal
            log(f"[{track_id}] (plot skipped: {e})")

    log(f"[{track_id}] {an.bpm:.2f} BPM | intro {a / sr:.2f}s | loop {(b - a) / sr:.2f}s "
        f"({len(bar_offsets)} bars) | outro {(y.shape[1] - b) / sr:.2f}s | "
        f"seam {an.loop_score:.3f} ({an.confidence})")
    for w in an.warnings:
        log(f"[{track_id}]   ! {w}")
    return manifest


def _plot(path, y, sr, an, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mono = y.mean(axis=0)
    step = max(1, len(mono) // 4000)
    env = np.abs(mono[: len(mono) // step * step]).reshape(-1, step).max(axis=1)
    t = np.arange(len(env)) * step / sr
    a, b = an.loop_start_sample / sr, an.loop_end_sample / sr
    fig, ax = plt.subplots(figsize=(14, 3.2), dpi=110)
    ax.fill_between(t, -env, env, color="#555", lw=0)
    ax.axvspan(0, a, color="#4c9be8", alpha=0.25, label="intro")
    ax.axvspan(a, b, color="#5cc46a", alpha=0.25, label="loop")
    ax.axvspan(b, len(mono) / sr, color="#e8834c", alpha=0.25, label="outro")
    for s in an.section_bars:
        if s < len(an.bar_samples):
            ax.axvline(an.bar_samples[s] / sr, color="k", lw=0.6, ls=":")
    ax.set_xlim(0, len(mono) / sr)
    ax.set_yticks([])
    ax.set_xlabel("seconds  (dotted = detected section boundaries)")
    ax.set_title(f"{title} - {an.bpm:.1f} BPM - loop bars {an.loop_start_bar}->{an.loop_end_bar} "
                 f"- seam {an.loop_score:.3f} ({an.confidence})")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
