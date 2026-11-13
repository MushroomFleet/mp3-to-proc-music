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
