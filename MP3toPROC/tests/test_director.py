"""Smoke tests for the runtime director. Run after converting the test songs:

    python tools/make_test_songs.py test_songs
    python -m mp3toproc batch test_songs -o proc_out
    copy examples/soundtrack.example.json proc_out/soundtrack.json
    python tests/test_director.py proc_out/soundtrack.json
"""
import sys

import numpy as np

sys.path.insert(0, ".")
from mp3toproc.director import MusicDirector  # noqa: E402


def run(d, seconds):
    out = [d.render(1024) for _ in range(int(seconds * d.sr / 1024))]
    return np.concatenate(out) if out else np.zeros((0, 2))


def main(st):
    ev = []
    d = MusicDirector(st, on_event=lambda k, i: ev.append((k, i)))
    a = run(d, 5)
    assert np.abs(a).max() > 0.01, "start track should be audible"
    assert d.current_track() == "forest_dawn"

    # exit quantized to the phrase grid inside the intro
    r = d.trigger("enter_cave")
    phrase = 4 * 4 * 60 / d.tracks["forest_dawn"].bpm
    rel = (d.tracks["forest_dawn"].length("intro") - r["exit"]) / d.sr
    assert abs(rel / phrase - round(rel / phrase)) < 0.01, rel

    # retarget before the new track is audible
    r2 = d.trigger("combat_start")
    assert r2 is not None and d.pending.track.id == "battle_rush"
    run(d, 3)
    assert d.current_track() == "battle_rush"

    # same-track trigger is a no-op
    assert d.trigger("combat_start") is None

    # @previous goes back to forest (cave was never audible)
    d.trigger("combat_end")
    run(d, 15)
    assert d.current_track() == "forest_dawn", d.current_track()

    # immediate override
    t0 = d.now
    r = d.trigger("boss_spotted")
    assert r["exit"] - t0 <= int(0.06 * d.sr)

    # long run: never silent for more than 1s while looping, never clipping hard
    x = run(d, 200)
    m = x.mean(axis=1)
    w = d.sr // 4
    rms = np.sqrt((m[: len(m) // w * w].reshape(-1, w) ** 2).mean(axis=1))
    assert (rms < 1e-3).sum() < 4, "unexpected silence"
    assert np.abs(x).max() < 1.5

    # retarget while the outro is already playing: outro must keep playing,
    # only the successor changes (same start time)
    d2 = MusicDirector(st)
    run(d2, 40)                                   # forest is now looping
    r = d2.trigger("enter_cave")
    while d2.now < r["exit"] + d2.sr:            # step past the exit, into the outro
        d2.render(1024)
    outro = [v for v in d2.voices if v.kind == "outro"]
    assert outro, "outro should be playing"
    r2 = d2.trigger("goto_battle" if "goto_battle" in d2.events else "battle_rush")
    assert r2["start"] == r["start"] and r2["exit"] == r["exit"], (r, r2)
    assert outro[0] in d2.voices, "outro must not be cut"
    assert d2.pending.track.id == "battle_rush"

    try:
        d.trigger("no_such_event")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    print("all director tests passed;", len(ev), "events")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "proc_out/soundtrack.json")
