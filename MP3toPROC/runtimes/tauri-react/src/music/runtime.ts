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
