"""MP3toPROC command line.

  python -m mp3toproc convert song.mp3 -o out/            one song
  python -m mp3toproc batch   music/   -o out/ -j 4       a whole folder
  python -m mp3toproc graph   out/                        write soundtrack.json skeleton
  python -m mp3toproc preview out/soundtrack.json events.json -o preview.wav
  python -m mp3toproc play    out/soundtrack.json         live audition (needs sounddevice)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed

AUDIO_EXT = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aiff", ".aif")


def _convert_job(args):
    src, out, kw = args
    warnings.filterwarnings("ignore")
    from .export import convert
    lines = []
    try:
        m = convert(src, out, log=lines.append, **kw)
        return src, m, lines, None
    except Exception as e:
        return src, None, lines, f"{type(e).__name__}: {e}"


def _kw(a, overrides=None):
    kw = dict(fmt=a.format, bpm=a.bpm, beats_per_bar=a.beats_per_bar,
              loop_xfade_ms=a.loop_xfade_ms, exit_every_bars=a.phrase_bars,
              crossfade_seconds=a.crossfade, plot=not a.no_plot,
              also_formats=tuple(a.also_format or ()))
    if overrides:
        kw.update(overrides)
    return kw


def cmd_convert(a):
    warnings.filterwarnings("ignore")
    from .export import convert
    kw = _kw(a)
    kw.update(track_id=a.id, loop_start_bar=a.loop_start_bar, loop_end_bar=a.loop_end_bar)
    convert(a.input, a.output, **kw)


def cmd_batch(a):
    files = sorted(f for f in glob.glob(os.path.join(a.input, "**", "*"), recursive=True)
                   if f.lower().endswith(AUDIO_EXT))
    if not files:
        sys.exit(f"No audio files found in {a.input}")
    overrides = {}
    if a.overrides:
        with open(a.overrides) as f:
            overrides = json.load(f)
    from .export import slugify
    jobs = []
    for src in files:
        o = overrides.get(os.path.basename(src)) or overrides.get(slugify(src)) or {}
        o = {k: v for k, v in o.items() if k in ("bpm", "loop_start_bar", "loop_end_bar", "track_id", "beats_per_bar")}
        if a.skip_existing and os.path.exists(os.path.join(a.output, o.get("track_id") or slugify(src), "track.json")):
            print(f"skip {os.path.basename(src)} (exists)")
            continue
        jobs.append((src, a.output, _kw(a, o)))
    print(f"Converting {len(jobs)} file(s) with {a.jobs} worker(s)...")
    failed = []
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for fut in as_completed([ex.submit(_convert_job, j) for j in jobs]):
            src, m, lines, err = fut.result()
            for line in lines:
                print(line)
            if err:
                print(f"FAILED {os.path.basename(src)}: {err}")
                failed.append(src)
    cmd_graph(argparse.Namespace(folder=a.output, force=False))
    print(f"Done. {len(jobs) - len(failed)} ok, {len(failed)} failed.")
    if failed:
        sys.exit(1)


def cmd_graph(a):
    path = os.path.join(a.folder, "soundtrack.json")
    tracks = {}
    for m in sorted(glob.glob(os.path.join(a.folder, "*", "track.json"))):
        tid = os.path.basename(os.path.dirname(m))
        tracks[tid] = os.path.relpath(m, a.folder).replace("\\", "/")
    if not tracks:
        sys.exit("No converted tracks found.")
    if os.path.exists(path) and not a.force:
        with open(path) as f:
            cfg = json.load(f)
        added = [t for t in tracks if t not in cfg["tracks"]]
        for t in added:
            cfg["tracks"][t] = tracks[t]
        print(f"soundtrack.json updated (+{len(added)} new track(s)); your rules were kept.")
    else:
        ids = list(tracks)
        cfg = {
            "sample_rate": 44100,
            "start": ids[0],
            "tracks": tracks,
            "defaults": {"mode": "outro", "quantize": "phrase", "crossfade_seconds": 4.0,
                         "outro_max_seconds": 16, "seam_seconds": 0.12, "lead_seconds": 0.05},
            "transitions": [
                {"from": "*", "to": ids[-1], "mode": "cut", "quantize": "bar",
                 "crossfade_seconds": 1.5, "skip_intro": True,
                 "_comment": "example: urgent switch (e.g. combat) - fast crossfade straight into the loop"}
            ],
            "events": {f"goto_{t}": t for t in ids} | {"back": "@previous"},
        }
        print(f"soundtrack.json written with {len(ids)} track(s).")
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def cmd_preview(a):
    warnings.filterwarnings("ignore")
    from .director import render_preview
    labels = a.labels or os.path.splitext(a.output)[0] + "_labels.txt"
    render_preview(a.soundtrack, a.script, a.output, labels, events_json=a.events_json)
    print(f"\nWrote {a.output} and {labels} (import labels in Audacity: File > Import > Labels)")


def cmd_play(a):
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("pip install sounddevice  (needed for live playback)")
    from .director import MusicDirector
    d = MusicDirector(a.soundtrack, on_event=lambda k, i: print(f"\n[{i['time']:.2f}s] {k} {i}"))

    def cb(outdata, frames, t, status):
        outdata[:] = d.render(frames)

    print("Events:", ", ".join(d.events) or "-", "| Tracks:", ", ".join(d.tracks))
    print("Type an event or track id and press Enter. 'q' quits.")
    with sd.OutputStream(samplerate=d.sr, channels=2, dtype="float32", callback=cb, blocksize=1024):
        while True:
            cmd = input("> ").strip()
            if cmd in ("q", "quit", "exit"):
                break
            if cmd:
                try:
                    d.trigger(cmd)
                except KeyError as e:
                    print(e)


def main(argv=None):
    p = argparse.ArgumentParser(prog="mp3toproc", description="MP3 -> procedural game soundtrack")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-o", "--output", default="proc_out", help="output folder")
        sp.add_argument("-f", "--format", choices=["wav", "ogg", "flac"], default="ogg")
        sp.add_argument("--bpm", type=float, help="tempo hint (fixes half/double-time detection)")
        sp.add_argument("--beats-per-bar", type=int, default=4)
        sp.add_argument("--phrase-bars", type=int, default=4, help="bars per phrase for exit points")
        sp.add_argument("--loop-xfade-ms", type=float, default=40.0, help="crossfade baked into loop seam")
        sp.add_argument("--crossfade", type=float, default=4.0, help="default track-to-track crossfade (s)")
        sp.add_argument("--no-plot", action="store_true")
        sp.add_argument("--also-format", action="append", choices=["wav", "ogg", "flac"],
                        help="also write segments in this format (e.g. flac fallback for older macOS webviews)")

    c = sub.add_parser("convert", help="convert one song")
    c.add_argument("input")
    c.add_argument("--id", help="track id (default: from file name)")
    c.add_argument("--loop-start-bar", type=int)
    c.add_argument("--loop-end-bar", type=int)
    common(c)
    c.set_defaults(fn=cmd_convert)

    b = sub.add_parser("batch", help="convert every audio file in a folder")
    b.add_argument("input")
    b.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    b.add_argument("--overrides", help="JSON: {\"file.mp3\": {\"loop_start_bar\": 16, \"loop_end_bar\": 48, \"bpm\": 128}}")
    b.add_argument("--skip-existing", action="store_true")
    common(b)
    b.set_defaults(fn=cmd_batch)

    g = sub.add_parser("graph", help="create/update soundtrack.json from converted tracks")
    g.add_argument("folder")
    g.add_argument("--force", action="store_true", help="overwrite existing soundtrack.json")
    g.set_defaults(fn=cmd_graph)

    v = sub.add_parser("preview", help="render a scripted play-through to WAV")
    v.add_argument("soundtrack")
    v.add_argument("script")
    v.add_argument("-o", "--output", default="preview.wav")
    v.add_argument("--labels")
    v.add_argument("--events-json", help="write the event timeline as JSON (golden file for engine ports)")
    v.set_defaults(fn=cmd_preview)

    pl = sub.add_parser("play", help="live audition with typed triggers")
    pl.add_argument("soundtrack")
    pl.set_defaults(fn=cmd_play)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
