"""Generate synthetic structured songs (intro / A / B / A / outro) as MP3s for testing.

Usage: python tools/make_test_songs.py <out_dir>
"""
import os
import subprocess
import sys

import numpy as np
import soundfile as sf

SR = 44100
rng = np.random.default_rng(7)


def tone(freq, dur, sr=SR, kind="saw", decay=None):
    t = np.arange(int(dur * sr)) / sr
    if kind == "sine":
        x = np.sin(2 * np.pi * freq * t)
    elif kind == "square":
        x = np.sign(np.sin(2 * np.pi * freq * t)) * 0.6
    else:
        x = 2 * ((freq * t) % 1.0) - 1
    if decay:
        x *= np.exp(-t / decay)
    return x


def kick(sr=SR):
    t = np.arange(int(0.25 * sr)) / sr
    f = 50 + 120 * np.exp(-t / 0.03)
    return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.12)


def hat(sr=SR):
    n = int(0.05 * sr)
    return rng.standard_normal(n) * np.exp(-np.arange(n) / (0.01 * sr)) * 0.3


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def place(buf, x, start):
    s = int(start)
    e = min(len(buf), s + len(x))
    if e > s:
        buf[s:e] += x[: e - s]


def make_song(bpm, prog_a, prog_b, intro_bars=8, outro_bars=8, a_bars=16, b_bars=16, drums_in_intro=False):
    beat = 60.0 / bpm
    bar = 4 * beat
    layout = [("intro", intro_bars), ("A", a_bars), ("B", b_bars), ("A", a_bars), ("outro", outro_bars)]
    total = sum(n for _, n in layout) * bar + 3.0
    L = np.zeros(int(total * SR))
    R = np.zeros(int(total * SR))
    t0 = 0.0
    for name, nbars in layout:
        prog = prog_b if name == "B" else prog_a
        for b in range(nbars):
            bt = t0 + b * bar
            chord = prog[b % len(prog)]
            # pad
            pad_gain = 0.08
            if name == "outro":
                pad_gain *= max(0.0, 1 - b / nbars)
            for n in chord:
                p = tone(midi(n), bar, kind="saw") * pad_gain
                p *= np.minimum(1, np.arange(len(p)) / (0.05 * SR))
                place(L, p, bt * SR)
                place(R, p * 0.9, bt * SR)
            full = name in ("A", "B") or (name == "intro" and drums_in_intro)
            fade = 1.0
            if name == "outro":
                fade = max(0.0, 1 - b / (nbars * 0.7))
                full = b < nbars * 0.7
            for q in range(4):
                qt = (bt + q * beat) * SR
                if full or (name == "intro" and b >= nbars // 2):
                    place(L, hat() * fade, qt + beat * SR / 2)
                    place(R, hat() * fade, qt + beat * SR / 2)
                if full:
                    k = kick() * 0.8 * fade
                    place(L, k, qt)
                    place(R, k, qt)
                    bn = tone(midi(chord[0] - 12), beat * 0.9, kind="square", decay=0.3) * 0.25 * fade
                    place(L, bn, qt)
                    place(R, bn, qt)
            if name in ("A", "B"):
                # simple lead melody, 8th notes
                for e in range(8):
                    n = chord[(e + b) % len(chord)] + 12 + (7 if (e == 5 and name == "B") else 0)
                    ld = tone(midi(n), beat / 2 * 0.95, kind="square", decay=0.15) * 0.07
                    place(L, ld * 0.7, (bt + e * beat / 2) * SR)
                    place(R, ld, (bt + e * beat / 2) * SR)
        t0 += nbars * bar
    y = np.stack([L, R], axis=1)
    y += rng.standard_normal(y.shape) * 0.002  # a little "room"
    y /= np.max(np.abs(y)) * 1.1
    return y


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    songs = {
        "Forest_Dawn": make_song(120, [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]],
                                 [[50, 53, 57], [52, 55, 59], [53, 57, 60], [55, 59, 62]]),
        "Cave_Echoes": make_song(96, [[50, 53, 57], [46, 50, 53], [48, 52, 55], [45, 49, 52]],
                                 [[43, 46, 50], [45, 48, 52], [46, 50, 53], [45, 49, 52]],
                                 intro_bars=4, outro_bars=6),
        "Battle_Rush": make_song(140, [[52, 55, 59], [48, 52, 55], [50, 53, 57], [47, 50, 54]],
                                 [[45, 48, 52], [47, 50, 54], [48, 52, 55], [50, 54, 57]],
                                 intro_bars=4, outro_bars=4, drums_in_intro=True),
    }
    for name, y in songs.items():
        wav = os.path.join(out_dir, name + ".wav")
        mp3 = os.path.join(out_dir, name + ".mp3")
        sf.write(wav, y, SR)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-b:a", "192k", mp3], check=True)
        os.remove(wav)
        print("wrote", mp3)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test_songs")
