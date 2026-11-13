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
