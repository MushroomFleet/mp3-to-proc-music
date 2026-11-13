// Loads soundtrack.json + track manifests, decodes audio on demand, and keeps
// decoded PCM within a memory budget (decoded audio is big: ~10.6 MB per
// stereo minute at 44.1 kHz float32).
import { buildGrid, withBufferDurations, type TrackGrid } from './quantize';
import {
  SUPPORTED_MANIFEST_VERSION,
  type SoundtrackConfig,
  type TrackManifest,
} from './types';

/** Maps a path relative to the music root ("forest/loop.ogg") to a fetchable URL. */
export type UrlResolver = (relPath: string) => string | Promise<string>;

export interface LoadedTrack {
  id: string;
  manifest: TrackManifest;
  grid: TrackGrid;
  gain: number;
  buffers: { intro: AudioBuffer | null; loop: AudioBuffer | null; outro: AudioBuffer | null };
  bytes: number;
}

interface Entry {
  manifest: TrackManifest;
  dir: string; // e.g. "forest_dawn/"
  gain: number;
  loading?: Promise<LoadedTrack>;
  loaded?: LoadedTrack;
  lastUsed: number;
}

const FALLBACK_EXT = ['.ogg', '.flac', '.wav'];

export class TrackLibrary {
  config!: SoundtrackConfig;
  private entries = new Map<string, Entry>();
  private pinned = new Set<string>();

  constructor(
    private readonly ctx: BaseAudioContext,
    private readonly resolve: UrlResolver,
    private readonly opts: { soundtrackPath?: string; budgetBytes?: number; fetchImpl?: typeof fetch } = {},
  ) {}

  private get fetch(): typeof fetch {
    return this.opts.fetchImpl ?? fetch.bind(globalThis);
  }

  async init(): Promise<SoundtrackConfig> {
    const stPath = this.opts.soundtrackPath ?? 'soundtrack.json';
    const base = stPath.includes('/') ? stPath.slice(0, stPath.lastIndexOf('/') + 1) : '';
    this.config = await this.json<SoundtrackConfig>(stPath);
    await Promise.all(
      Object.entries(this.config.tracks).map(async ([id, ref]) => {
        const mPath = base + (typeof ref === 'string' ? ref : ref.manifest);
        const manifest = await this.json<TrackManifest>(mPath);
        if (manifest.mp3toproc_version > SUPPORTED_MANIFEST_VERSION) {
          throw new Error(`[music] ${id}: manifest v${manifest.mp3toproc_version} is newer than this runtime supports`);
        }
        if (manifest.analysis?.confidence === 'low') {
          console.warn(`[music] ${id}: loop seam confidence is LOW, ask the audio team to audition it`);
        }
        this.entries.set(id, {
          manifest,
          dir: mPath.slice(0, mPath.lastIndexOf('/') + 1),
          gain: typeof ref === 'string' ? 1 : ref.gain ?? 1,
          lastUsed: 0,
        });
      }),
    );
    return this.config;
  }

  has(id: string): boolean {
    return this.entries.has(id);
  }

  ids(): string[] {
    return [...this.entries.keys()];
  }

  /** Keep these tracks decoded (current + pending). Others may be evicted. */
  pin(ids: Iterable<string>): void {
    this.pinned = new Set(ids);
  }

  isLoaded(id: string): boolean {
    return !!this.entries.get(id)?.loaded;
  }

  preload(ids: string[]): Promise<unknown> {
    return Promise.allSettled(ids.map((id) => this.ensure(id)));
  }

  ensure(id: string): Promise<LoadedTrack> {
    const e = this.entries.get(id);
    if (!e) return Promise.reject(new Error(`[music] unknown track "${id}"`));
    e.lastUsed = performance.now();
    if (e.loaded) return Promise.resolve(e.loaded);
    if (!e.loading) {
      e.loading = this.decodeTrack(id, e).then(
        (t) => {
          e.loaded = t;
          e.loading = undefined;
          this.evict();
          return t;
        },
        (err) => {
          e.loading = undefined;
          throw err;
        },
      );
    }
    return e.loading;
  }

  private async decodeTrack(id: string, e: Entry): Promise<LoadedTrack> {
    const seg = e.manifest.segments;
    const [intro, loop, outro] = await Promise.all([
      this.decodeSegment(e.dir, seg.intro.file),
      this.decodeSegment(e.dir, seg.loop.file),
      this.decodeSegment(e.dir, seg.outro.file),
    ]);
    if (!loop) throw new Error(`[music] ${id}: loop segment missing`);
    const grid = withBufferDurations(buildGrid(e.manifest), {
      intro: intro?.duration ?? 0,
      loop: loop.duration,
      outro: outro?.duration ?? 0,
    });
    const bytes = [intro, loop, outro].reduce((n, b) => n + (b ? b.length * b.numberOfChannels * 4 : 0), 0);
    return { id, manifest: e.manifest, grid, gain: e.gain, buffers: { intro, loop, outro }, bytes };
  }

  /** Tries the manifest's file first, then the same stem with other extensions.
   *  This covers webviews that can't decode Ogg (e.g. macOS < 15.4). */
  private async decodeSegment(dir: string, file: string | null): Promise<AudioBuffer | null> {
    if (!file) return null;
    const stem = file.replace(/\.[^.]+$/, '');
    const ext = file.slice(stem.length);
    const candidates = [file, ...FALLBACK_EXT.filter((x) => x !== ext).map((x) => stem + x)];
    let lastErr: unknown;
    for (const f of candidates) {
      try {
        const res = await this.fetch(await this.resolve(dir + f));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.arrayBuffer();
        return await this.ctx.decodeAudioData(data);
      } catch (err) {
        lastErr = err;
      }
    }
    throw new Error(`[music] could not load/decode ${dir}${file} (tried ${candidates.join(', ')}): ${String(lastErr)}`);
  }

  private evict(): void {
    const budget = this.opts.budgetBytes ?? 512 * 1024 * 1024;
    const loaded = [...this.entries.entries()].filter(([, e]) => e.loaded);
    let total = loaded.reduce((n, [, e]) => n + e.loaded!.bytes, 0);
    loaded.sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    for (const [id, e] of loaded) {
      if (total <= budget) break;
      if (this.pinned.has(id)) continue;
      total -= e.loaded!.bytes;
      e.loaded = undefined;
    }
  }

  private async json<T>(rel: string): Promise<T> {
    const res = await this.fetch(await this.resolve(rel));
    if (!res.ok) throw new Error(`[music] ${rel}: HTTP ${res.status}`);
    return (await res.json()) as T;
  }
}
