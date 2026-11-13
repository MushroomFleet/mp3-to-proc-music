"""Musical structure analysis for MP3toPROC.

Finds tempo, bar grid, section boundaries, the intro / outro regions, and the
best seamless loop (start bar A, end bar B) such that jumping from B back to A
is musically invisible.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional

import numpy as np
import librosa
import scipy.signal

ANALYSIS_SR = 22050
HOP = 512


@dataclass
class Analysis:
    sr: int                       # native sample rate of the source
    n_samples: int
    bpm: float
    beats_per_bar: int
    beat_samples: List[int]       # beat positions (native samples)
    bar_samples: List[int]        # downbeat positions (native samples)
    bar_energy_db: List[float]
    section_bars: List[int]       # bar indices where a new section starts
    intro_end_bar: int
    outro_start_bar: int
    loop_start_bar: int
    loop_end_bar: int
    loop_start_sample: int
    loop_end_sample: int
    loop_score: float
    fwd_similarity: float
    back_similarity: float
    confidence: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------- helpers

def _zscore(F):
    mu = F.mean(axis=1, keepdims=True)
    sd = F.std(axis=1, keepdims=True) + 1e-8
    return (F - mu) / sd


def _cos(a, b):
    a = a.ravel()
    b = b.ravel()
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _novelty(S, k):
    """Foote checkerboard novelty on a self-similarity matrix."""
    n = S.shape[0]
    if n < 2 * k + 1:
        return np.zeros(n)
    g = scipy.signal.windows.gaussian(2 * k, std=k / 2)
    G = np.outer(g, g)
    sign = np.ones((2 * k, 2 * k))
    sign[:k, k:] = -1
    sign[k:, :k] = -1
    K = G * sign
    Sp = np.pad(S, k, mode="edge")
    nov = np.array([np.sum(Sp[i:i + 2 * k, i:i + 2 * k] * K) for i in range(n)])
    nov = np.maximum(nov, 0)
    return nov / (nov.max() + 1e-9)


# --------------------------------------------------------------------------- main

def analyze(y_native: np.ndarray, sr: int, *, bpm_hint: Optional[float] = None,
            beats_per_bar: int = 4, loop_start_bar: Optional[int] = None,
            loop_end_bar: Optional[int] = None, min_loop_bars: int = 8,
            match_beats: int = 8) -> Analysis:
    """y_native: float array (channels, samples) or (samples,)."""
    warnings: List[str] = []
    mono_native = y_native if y_native.ndim == 1 else y_native.mean(axis=0)
    n_samples = len(mono_native)
    y = librosa.resample(mono_native, orig_sr=sr, target_sr=ANALYSIS_SR)
    scale = sr / ANALYSIS_SR

    # ---- tempo & beats -------------------------------------------------------
    onset_env = librosa.onset.onset_strength(y=y, sr=ANALYSIS_SR, hop_length=HOP)
    kw = {}
    if bpm_hint:
        kw = dict(start_bpm=bpm_hint, tightness=400)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=ANALYSIS_SR,
                                           hop_length=HOP, units="frames", **kw)
    bpm = float(np.atleast_1d(tempo)[0])
    if len(beats) < beats_per_bar * (min_loop_bars + 2):
        raise RuntimeError("Not enough beats detected - is this track rhythmic? "
                           "Try --bpm, or set loop points manually.")

    # Regularise the beat grid: fit a straight line (constant tempo) and use it
    # where the tracker drifts. Most produced game/EDM music is fixed tempo.
    idx = np.arange(len(beats))
    slope, icpt = np.polyfit(idx, beats.astype(float), 1)
    resid = beats - (slope * idx + icpt)
    low = librosa.onset.onset_strength(y=y, sr=ANALYSIS_SR, hop_length=HOP, fmax=200)
    if np.std(resid) < 2.0:  # within ~2 frames => constant tempo track
        bpm = 60.0 * ANALYSIS_SR / (HOP * slope)
        # Phase refinement: the tracker can lock onto off-beat hats. Slide the grid
        # within +-half a beat to sit on the strongest (kick-weighted) onsets.
        drive = onset_env / (onset_env.max() + 1e-9) + 2 * low / (low.max() + 1e-9)
        fr = np.arange(len(drive))
        # Joint search over phase and a tiny tempo correction (+-0.4%), pivoting
        # around the middle of the track so tempo tweaks don't move the phase.
        mid_k = len(beats) // 2
        pivot = icpt + mid_k * slope
        best = (-1.0, pivot, slope)
        for sl in slope * (1 + np.linspace(-0.004, 0.004, 33)):
            for o in np.linspace(-sl / 2, sl / 2, 41):
                c = pivot + o
                k = np.arange(np.ceil(-c / sl), np.floor((len(drive) - 1 - c) / sl) + 1)
                v = np.interp(c + k * sl, fr, drive).mean()
                if v > best[0]:
                    best = (v, c, sl)
        _, c, slope = best
        bpm = 60.0 * ANALYSIS_SR / (HOP * slope)
        # Extend the constant-tempo grid over the whole file (quiet intros/outros
        # often get trimmed by the tracker). A beat a hair before t=0 is kept at 0.
        n_on = len(onset_env)
        k = np.arange(np.ceil((-c - 0.25 * slope) / slope), np.floor((n_on - 1 - c) / slope) + 1)
        beat_times = librosa.frames_to_time(c + k * slope, sr=ANALYSIS_SR, hop_length=HOP)
        # Onset envelopes peak slightly after the true attack. Shift the grid by the
        # median distance to the nearest backtracked (attack-start) onset.
        on = librosa.onset.onset_detect(y=y, sr=ANALYSIS_SR, hop_length=128, backtrack=True, units="time")
        if len(on):
            j = np.clip(np.searchsorted(on, beat_times), 1, len(on) - 1)
            near = np.where(np.abs(on[j - 1] - beat_times) < np.abs(on[j] - beat_times), on[j - 1], on[j])
            d = near - beat_times
            d = d[np.abs(d) < 0.07]
            if len(d) > 8:
                beat_times = beat_times + float(np.median(d))
        beat_times = np.maximum(0.0, beat_times)
    else:
        warnings.append("Tempo appears to vary; using raw beat tracker grid.")
        beat_times = librosa.frames_to_time(beats, sr=ANALYSIS_SR, hop_length=HOP)

    # ---- beat-synchronous features ------------------------------------------
    chroma = librosa.feature.chroma_cqt(y=y, sr=ANALYSIS_SR, hop_length=HOP)
    mfcc = librosa.feature.mfcc(y=y, sr=ANALYSIS_SR, hop_length=HOP, n_mfcc=13)
    rms = librosa.feature.rms(y=y, hop_length=HOP)
    beat_frames = librosa.time_to_frames(beat_times, sr=ANALYSIS_SR, hop_length=HOP)
    n_frames = chroma.shape[1]
    bf = np.clip(beat_frames, 0, n_frames - 1)
    bounds = np.concatenate([bf, [n_frames]])
    Fc = librosa.util.sync(chroma, bounds, aggregate=np.mean, pad=False)
    Fm = librosa.util.sync(mfcc, bounds, aggregate=np.mean, pad=False)
    Fr = librosa.util.sync(rms, bounds, aggregate=np.mean, pad=False)
    F = np.vstack([_zscore(Fc) * 1.0, _zscore(Fm[1:]) * 0.8, _zscore(np.log(Fr + 1e-6)) * 1.5])

    # ---- downbeat phase -----------------------------------------------------
    # Downbeats tend to have the biggest spectral/harmonic change & low-end hit.
    harm_change = np.r_[0, np.linalg.norm(np.diff(Fc, axis=1), axis=0)]
    best_phase, best_val = 0, -1
    for p in range(beats_per_bar):
        sel = np.arange(p, len(bf), beats_per_bar)
        v = low[bf[sel]].mean() / (low.mean() + 1e-9) + harm_change[sel].mean() / (harm_change.mean() + 1e-9)
        if v > best_val:
            best_phase, best_val = p, v
    bar_beat_idx = np.arange(best_phase, len(bf), beats_per_bar)
    n_bars = len(bar_beat_idx)

    beat_samples = np.round(beat_times * sr).astype(int)
    bar_samples = beat_samples[bar_beat_idx]

    # ---- bar-level features, energy, sections -------------------------------
    Fbar = np.stack([F[:, i:i + beats_per_bar].mean(axis=1) for i in bar_beat_idx], axis=1)
    bar_rms = np.array([Fr[0, i:i + beats_per_bar].mean() for i in bar_beat_idx])
    bar_db = 20 * np.log10(bar_rms + 1e-6)
    Fn = Fbar / (np.linalg.norm(Fbar, axis=0, keepdims=True) + 1e-9)
    S = Fn.T @ Fn
    nov = _novelty(S, k=min(4, max(2, n_bars // 8)))
    peaks, _ = scipy.signal.find_peaks(nov, height=0.15, distance=4)
    section_bars = sorted(set([0] + [int(p) for p in peaks]))

    # ---- intro / outro detection --------------------------------------------
    sm = np.convolve(np.pad(bar_db, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    hi = np.percentile(sm, 70)
    thresh = hi - 3.0
    loud = np.where(sm >= thresh)[0]
    first_loud = int(loud[0]) if len(loud) else 0
    last_loud = int(loud[-1]) if len(loud) else n_bars - 1

    def snap(b, lo, hi_):
        cands = [s for s in section_bars + [n_bars] if lo <= s <= hi_]
        return min(cands, key=lambda s: abs(s - b)) if cands else b

    intro_end = snap(first_loud, max(0, first_loud - 4), first_loud + 4)
    outro_start = snap(last_loud + 1, last_loud - 3, min(n_bars, last_loud + 5))
    if outro_start - intro_end < min_loop_bars:
        warnings.append("Could not separate intro/outro clearly; using whole song body.")
        intro_end, outro_start = 0, n_bars

    # ---- loop search --------------------------------------------------------
    W = match_beats
    nb = F.shape[1]

    def win(i, back=False):
        if back:
            return F[:, i - W:i] if i - W >= 0 else None
        return F[:, i:i + W] if i + W <= nb else None

    def score_pair(a_bar, b_bar):
        ia, ib = bar_beat_idx[a_bar], (bar_beat_idx[b_bar] if b_bar < n_bars else nb)
        fa, fb = win(ia), win(ib)
        ba, bb = win(ia, True), win(ib, True)
        fwd = _cos(fa, fb) if fa is not None and fb is not None else None
        back = _cos(ba, bb) if ba is not None and bb is not None else None
        if fwd is None and back is None:
            return -1, 0, 0
        if fwd is None:
            fwd = back
        if back is None:
            back = fwd
        return 0.6 * fwd + 0.4 * back, fwd, back

    if loop_start_bar is not None and loop_end_bar is not None:
        a_best, b_best = int(loop_start_bar), int(loop_end_bar)
        s_best, fwd_best, back_best = score_pair(a_best, b_best)
    else:
        max_len = max(1, outro_start - intro_end)
        search = max(8, min(24, max_len // 3))
        a_range = range(max(0, intro_end - 2), min(n_bars, intro_end + search))
        b_range = range(max(0, outro_start - search), min(n_bars, outro_start + 2) + 1)
        if loop_start_bar is not None:
            a_range = [int(loop_start_bar)]
        if loop_end_bar is not None:
            b_range = [int(loop_end_bar)]
        best = None
        for a in a_range:
            for b in b_range:
                L = b - a
                if L < min_loop_bars or L % 2:
                    continue
                s, fwd, back = score_pair(a, b)
                bonus = 0.08 * (L / max_len)
                bonus += 0.03 if L % 16 == 0 else 0.015 if L % 8 == 0 else 0.005 if L % 4 == 0 else 0
                bonus -= 0.004 * abs(a - intro_end) / max(1, search)   # stay near intro boundary
                rel = a - intro_end                                      # keep phrase alignment
                bonus += 0.008 if rel % 8 == 0 else 0.005 if rel % 4 == 0 else 0
                total = s + bonus
                if best is None or total > best[0]:
                    best = (total, a, b, s, fwd, back)
        if best is None:
            raise RuntimeError("No valid loop found; track may be too short.")
        _, a_best, b_best, s_best, fwd_best, back_best = best

    conf = "high" if s_best > 0.85 else "medium" if s_best > 0.65 else "low"
    if conf == "low":
        warnings.append("Low loop similarity - audition the seam; consider manual loop bars.")

    a_s = int(bar_samples[a_best])
    b_s = int(bar_samples[b_best]) if b_best < n_bars else n_samples
    a_s, b_s = refine_loop_points(mono_native, sr, a_s, b_s)

    return Analysis(
        sr=sr, n_samples=n_samples, bpm=round(bpm, 3), beats_per_bar=beats_per_bar,
        beat_samples=[int(x) for x in beat_samples], bar_samples=[int(x) for x in bar_samples],
        bar_energy_db=[round(float(x), 2) for x in bar_db], section_bars=section_bars,
        intro_end_bar=int(intro_end), outro_start_bar=int(outro_start),
        loop_start_bar=int(a_best), loop_end_bar=int(b_best),
        loop_start_sample=a_s, loop_end_sample=b_s,
        loop_score=round(float(s_best), 4), fwd_similarity=round(float(fwd_best), 4),
        back_similarity=round(float(back_best), 4), confidence=conf, warnings=warnings,
    )


def refine_loop_points(mono: np.ndarray, sr: int, a: int, b: int):
    """Sample-accurate refinement.

    1. Snap A to the nearest transient (within +-25 ms) so the loop starts on the hit.
    2. Slide B (+-25 ms) to maximise waveform correlation of the audio following B
       with the audio following A, so the jump B->A is phase-aligned.
    """
    n = len(mono)
    hop = 64
    rad = int(0.025 * sr)
    lo, hi = max(0, a - rad - 2048), min(n, a + rad + 2048)
    seg = mono[lo:hi]
    if len(seg) > 4096:
        env = librosa.onset.onset_strength(y=seg, sr=sr, hop_length=hop)
        times = lo + np.arange(len(env)) * hop
        mask = np.abs(times - a) <= rad
        if mask.any() and env[mask].max() > 1.5 * (np.median(env) + 1e-9):
            a = int(times[mask][np.argmax(env[mask])])

    N = int(0.25 * sr)
    if a + N < n and b + rad + N < n and b - rad > a:
        ref = mono[a:a + N]
        best_d, best_c = 0, -np.inf
        for d in range(-rad, rad + 1, 4):
            c = np.dot(ref, mono[b + d:b + d + N])
            if c > best_c:
                best_c, best_d = c, d
        for d in range(best_d - 4, best_d + 5):  # fine pass
            c = np.dot(ref, mono[b + d:b + d + N])
            if c > best_c:
                best_c, best_d = c, d
        b = b + best_d
    return int(a), int(b)
