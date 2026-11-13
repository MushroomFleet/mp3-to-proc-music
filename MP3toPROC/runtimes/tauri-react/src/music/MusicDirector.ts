// Runtime half of MP3toPROC for Web Audio (Tauri webview, browsers).
// TypeScript port of mp3toproc/director.py; produces the same exit/start
// times for the same inputs (see the parity test).
//
//   intro -> loop (native looping AudioBufferSourceNode) -> [trigger]
//         -> exit at the next quantized point -> outro  ⟶ crossfade ⟶  next intro -> ...
//
// All audio is scheduled ahead on the AudioContext clock, so it is
// sample-accurate even if the JS thread stalls (GC, heavy frame, hidden
// window). JS timers are only used for bookkeeping (which track is current, cleanup).
import { Envelope } from './envelope';
import type { LoadedTrack, TrackLibrary } from './library';
import { exitPoint, isOnSeam, loopPosition } from './quantize';
import { DEFAULT_RULE, type TransitionRule } from './types';

type Kind = 'intro' | 'loop' | 'outro';

interface Voice {
  kind: Kind;
  node: AudioBufferSourceNode;
  gain: GainNode;
  env: Envelope;
  start: number; // ctx time buffer position `offset` plays
  offset: number;
}

interface Deck {
  track: LoadedTrack;
  start: number;
  loopStart: number;
  out: GainNode; // deck crossfade gain
  trim: GainNode; // per-track gain from soundtrack.json
  env: Envelope;
  fade: number; // fade-in length the deck started with
  voices: Voice[];
  exitAt: number | null;
  endAt: number; // when the deck is silent and can be disposed
}

export type DirectorEvent =
  | { type: 'track_started'; track: string; time: number }
  | {
      type: 'exit_scheduled';
      trigger: string;
      source: string;
      target: string;
      mode: 'outro' | 'cut' | 'retarget';
      quantize: string;
      exit: number;
      nextStart: number;
    }
  | { type: 'trigger_ignored'; trigger: string; reason: string }
  | { type: 'error'; error: unknown };

export interface ScheduleResult {
  exit: number;
  start: number;
}

export interface DirectorState {
  current: string | null;
  pending: string | null;
  phase: 'idle' | 'intro' | 'loop' | 'outro';
  loopPosition: number | null;
  nextExit: Record<'beat' | 'bar' | 'phrase', number> | null;
}

export class MusicDirector {
  readonly bus: GainNode;
  private busEnv: Envelope;
  private deck: Deck | null = null;
  private pending: Deck | null = null;
  private retired: Deck[] = [];
  private history: string[] = [];
  private listeners = new Set<(e: DirectorEvent) => void>();
  private requestSeq = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly defaults: TransitionRule;

  constructor(
    private readonly ctx: BaseAudioContext,
    private readonly lib: TrackLibrary,
    opts: { destination?: AudioNode; autoTick?: boolean } = {},
  ) {
    this.defaults = { ...DEFAULT_RULE, ...(lib.config.defaults ?? {}) };
    this.bus = ctx.createGain();
    this.busEnv = new Envelope(this.bus.gain, 1, ctx.currentTime);
    this.bus.connect(opts.destination ?? ctx.destination);
    if (opts.autoTick !== false) this.timer = setInterval(() => this.tick(), 50);
  }

  // ------------------------------------------------------------------ public API

  on(fn: (e: DirectorEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  get current(): string | null {
    return (this.pending ?? this.deck)?.track.id ?? null;
  }

  /** Start playback (call after the AudioContext is running). */
  async start(id = this.lib.config.start, at?: number): Promise<void> {
    if (!id) throw new Error('[music] no start track');
    const track = await this.lib.ensure(id);
    const t = at ?? this.ctx.currentTime + this.defaults.lead_seconds;
    this.deck = this.startDeck(track, t, this.defaults, 0);
    this.history.push(id);
    this.pinCurrent();
    this.emit({ type: 'track_started', track: id, time: t });
  }

  /** Game-facing: event name from soundtrack.json, or a track id. Safe to spam. */
  async trigger(name: string): Promise<ScheduleResult | null> {
    let resolved: { target: string; override: Partial<TransitionRule> } | null;
    try {
      resolved = this.resolve(name);
    } catch (error) {
      this.emit({ type: 'error', error });
      throw error;
    }
    if (!resolved) {
      this.emit({ type: 'trigger_ignored', trigger: name, reason: 'no previous track' });
      return null;
    }
    const seq = ++this.requestSeq;
    const track = await this.lib.ensure(resolved.target); // usually already preloaded
    if (seq !== this.requestSeq) return null; // a newer trigger superseded this one
    return this.request(track, resolved.override, name);
  }

  /** Bookkeeping: promote the pending deck when it becomes audible, free finished decks. */
  tick(): void {
    const now = this.ctx.currentTime;
    if (this.pending && now >= this.pending.start) this.promote();
    this.retired = this.retired.filter((d) => {
      if (now < d.endAt + 0.25) return true;
      this.disposeDeck(d);
      return false;
    });
  }

  setVolume(v: number, fadeSeconds = 0.5): void {
    this.busEnv.fade(this.ctx.currentTime, fadeSeconds, v);
  }

  /** Fade everything out (e.g. game over / back to menu). */
  stopAll(fadeSeconds = 1): void {
    const now = this.ctx.currentTime;
    for (const d of [this.deck, this.pending]) {
      if (!d) continue;
      d.env.fade(now, fadeSeconds, 0);
      for (const v of d.voices) safeStop(v.node, now + fadeSeconds);
      d.endAt = now + fadeSeconds;
      d.exitAt = now;
      this.retired.push(d);
    }
    this.deck = this.pending = null;
  }

  getState(): DirectorState {
    const d = this.deck;
    const now = this.ctx.currentTime;
    if (!d) return { current: null, pending: null, phase: 'idle', loopPosition: null, nextExit: null };
    const g = d.track.grid;
    const phase = d.exitAt !== null && now >= d.exitAt ? 'outro' : now < d.loopStart ? 'intro' : 'loop';
    return {
      current: d.track.id,
      pending: this.pending?.track.id ?? null,
      phase,
      loopPosition: loopPosition(g, d.loopStart, now),
      nextExit: {
        beat: exitPoint(g, d.loopStart, 'beat', now),
        bar: exitPoint(g, d.loopStart, 'bar', now),
        phrase: exitPoint(g, d.loopStart, 'phrase', now),
      },
    };
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    for (const d of [this.deck, this.pending, ...this.retired]) if (d) this.disposeDeck(d);
    this.deck = this.pending = null;
    this.retired = [];
    this.bus.disconnect();
    this.listeners.clear();
  }

  // ------------------------------------------------------------------ internals

  private emit(e: DirectorEvent): void {
    for (const fn of this.listeners) fn(e);
  }

  private resolve(name: string): { target: string; override: Partial<TransitionRule> } | null {
    const ev = this.lib.config.events?.[name];
    let target: string;
    let override: Partial<TransitionRule> = {};
    if (ev === undefined) target = name;
    else if (typeof ev === 'string') target = ev;
    else {
      const { target: t, ...rest } = ev;
      target = t;
      override = rest;
    }
    if (target === '@previous') {
      const cur = this.current;
      const prev = [...this.history].reverse().find((t) => t !== cur);
      if (!prev) return null;
      target = prev;
    }
    if (!this.lib.has(target)) throw new Error(`[music] unknown event or track "${name}"`);
    return { target, override };
  }

  private rule(src: string | null, dst: string, override: Partial<TransitionRule>): TransitionRule {
    const r: TransitionRule = { ...this.defaults };
    for (const rule of this.lib.config.transitions ?? []) {
      const from = rule.from ?? '*';
      const to = rule.to ?? '*';
      if ((from === '*' || from === src) && (to === '*' || to === dst)) {
        const { from: _f, to: _t, _comment, ...rest } = rule;
        Object.assign(r, rest);
      }
    }
    return Object.assign(r, override);
  }

  private request(target: LoadedTrack, override: Partial<TransitionRule>, name: string): ScheduleResult | null {
    const now = this.ctx.currentTime;
    const lead = this.defaults.lead_seconds;
    this.tick();

    if (this.pending) {
      const src = this.deck!;
      const old = this.pending;
      if (old.track.id === target.id) return null;
      if (old.start > now + lead) {
        this.killDeck(old, now);
        this.pending = null;
        if (src.exitAt !== null && src.exitAt > now + lead) {
          // The exit hasn't happened yet: undo it and recompute from scratch.
          this.rewind(src, src.exitAt);
        } else {
          // The outro is already playing: keep it, only swap what comes next.
          const rule = this.rule(src.track.id, target.id, override);
          this.pending = this.startDeck(target, old.start, rule, old.fade);
          this.pinCurrent();
          this.emit({
            type: 'exit_scheduled', trigger: name, source: src.track.id, target: target.id,
            mode: 'retarget', quantize: '-', exit: src.exitAt ?? now, nextStart: old.start,
          });
          return { exit: src.exitAt ?? now, start: old.start };
        }
      } else {
        this.promote(); // successor is (about to be) audible: treat it as current
      }
    }

    const cur = this.deck;
    if (!cur) {
      const t = now + lead;
      this.deck = this.startDeck(target, t, this.defaults, 0);
      this.history.push(target.id);
      this.pinCurrent();
      this.emit({ type: 'track_started', track: target.id, time: t });
      return { exit: t, start: t };
    }
    if (cur.track.id === target.id && cur.exitAt === null) return null;
    return this.commitExit(cur, target, this.rule(cur.track.id, target.id, override), now + lead, name);
  }

  private commitExit(d: Deck, target: LoadedTrack, rule: TransitionRule, earliest: number, name: string): ScheduleResult {
    const g = d.track.grid;
    const E = exitPoint(g, d.loopStart, rule.quantize, earliest);
    let mode = rule.mode;
    const outroBuf = d.track.buffers.outro;
    if (mode === 'outro' && !outroBuf) mode = 'cut';
    d.exitAt = E;

    // Voices of this deck that are sounding at E (anything starting at/after E never plays).
    const playing: Voice[] = [];
    for (const v of d.voices) {
      if (v.kind === 'outro') continue;
      if (v.start >= E - 1e-6) {
        safeStop(v.node, E);
        continue;
      }
      const end = v.kind === 'loop' ? Infinity : v.start + v.node.buffer!.duration - v.offset;
      if (end <= E + 1e-6) continue; // intro that ends exactly at E
      playing.push(v);
    }

    let S: number;
    if (mode === 'outro') {
      let off = 0;
      if (rule.outro_from === 'tail' && g.outroTail > 0 && g.outroTail < outroBuf!.duration) off = g.outroTail;
      const seamless = off === 0 && isOnSeam(g, d.loopStart, E);
      const seam = seamless ? 0 : rule.seam_seconds;
      for (const v of playing) {
        if (seam > 0) v.env.fade(E, seam, 0);
        safeStop(v.node, E + seam);
      }
      let outroLen = outroBuf!.duration - off;
      if (rule.outro_max_seconds) outroLen = Math.min(outroLen, rule.outro_max_seconds);
      const xf = Math.min(rule.crossfade_seconds, outroLen);
      S = E + outroLen - xf;
      const ov = this.addVoice(d, 'outro', outroBuf!, E, off, false, seam > 0 ? 0 : 1);
      if (seam > 0) ov.env.fade(E, seam, 1);
      safeStop(ov.node, E + outroLen);
      if (xf > 0) d.env.fade(S, xf, 0);
      d.endAt = E + outroLen;
      this.pending = this.startDeck(target, S, rule, xf);
    } else {
      const xf = rule.crossfade_seconds;
      if (xf > 0) d.env.fade(E, xf, 0);
      for (const v of playing) safeStop(v.node, E + xf);
      d.endAt = E + xf;
      S = E;
      this.pending = this.startDeck(target, S, rule, xf);
    }
    this.retired.push(d);
    this.pinCurrent();
    this.emit({
      type: 'exit_scheduled', trigger: name, source: d.track.id, target: target.id,
      mode, quantize: rule.quantize, exit: E, nextStart: S,
    });
    return { exit: E, start: S };
  }

  /** Undo a scheduled (not yet reached) exit at time T and keep the deck playing. */
  private rewind(d: Deck, T: number): void {
    for (const v of d.voices) safeStop(v.node, T); // re-stop earlier; outro never starts
    d.voices = [];
    d.env.cancel(T);
    if (d.env.valueAt(T) < 0.999) d.env.fade(T, Math.max(0.05, d.start + d.fade - T), 1);
    const b = d.track.buffers;
    if (T < d.loopStart && b.intro) {
      this.addVoice(d, 'intro', b.intro, T, T - d.start, false);
      this.addVoice(d, 'loop', b.loop!, d.loopStart, 0, true);
    } else {
      const pos = loopPosition(d.track.grid, d.loopStart, T) ?? 0;
      this.addVoice(d, 'loop', b.loop!, T, pos, true);
    }
    d.exitAt = null;
    d.endAt = Infinity;
    this.retired = this.retired.filter((x) => x !== d);
  }

  private startDeck(track: LoadedTrack, at: number, rule: TransitionRule, fade: number): Deck {
    const out = this.ctx.createGain();
    const trim = this.ctx.createGain();
    trim.gain.value = track.gain;
    out.connect(trim).connect(this.bus);
    const env = new Envelope(out.gain, fade > 0 ? 0 : 1, this.ctx.currentTime);
    if (fade > 0) env.fade(at, fade, 1);
    const skip = rule.skip_intro || !track.buffers.intro;
    const loopStart = skip ? at : at + track.grid.introDur;
    const d: Deck = { track, start: at, loopStart, out, trim, env, fade, voices: [], exitAt: null, endAt: Infinity };
    if (!skip) this.addVoice(d, 'intro', track.buffers.intro!, at, 0, false);
    this.addVoice(d, 'loop', track.buffers.loop!, loopStart, 0, true);
    return d;
  }

  private addVoice(d: Deck, kind: Kind, buffer: AudioBuffer, when: number, offset: number, loop: boolean, gain = 1): Voice {
    const node = this.ctx.createBufferSource();
    node.buffer = buffer;
    node.loop = loop; // loops the whole buffer; the seam is baked into the file
    const g = this.ctx.createGain();
    node.connect(g).connect(d.out);
    const env = new Envelope(g.gain, gain, this.ctx.currentTime);
    node.onended = () => {
      node.disconnect();
      g.disconnect();
    };
    node.start(when, offset);
    const v: Voice = { kind, node, gain: g, env, start: when, offset };
    d.voices.push(v);
    return v;
  }

  private promote(): void {
    if (!this.pending) return;
    this.deck = this.pending;
    this.pending = null;
    this.history.push(this.deck.track.id);
    if (this.history.length > 32) this.history.shift();
    this.pinCurrent();
    this.emit({ type: 'track_started', track: this.deck.track.id, time: this.deck.start });
  }

  private killDeck(d: Deck, at: number): void {
    for (const v of d.voices) safeStop(v.node, at);
    d.endAt = at;
    this.retired.push(d);
  }

  private disposeDeck(d: Deck): void {
    for (const v of d.voices) {
      safeStop(v.node, 0);
      v.node.disconnect();
      v.gain.disconnect();
    }
    d.out.disconnect();
    d.trim.disconnect();
  }

  private pinCurrent(): void {
    this.lib.pin([this.deck?.track.id, this.pending?.track.id].filter((x): x is string => !!x));
  }
}

function safeStop(node: AudioScheduledSourceNode, when: number): void {
  try {
    node.stop(when);
  } catch {
    /* already stopped / never started: fine */
  }
}
