// React glue: provider, hook, and the R3F <MusicZone>.
import { useFrame } from '@react-three/fiber';
import {
  createContext, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore,
  type ReactNode, type RefObject,
} from 'react';
import * as THREE from 'three';
import type { DirectorEvent, MusicDirector } from './MusicDirector';
import type { UrlResolver } from './library';
import { initMusic, installAudioUnlock, type MusicRuntime } from './runtime';

interface MusicCtx {
  runtime: MusicRuntime | null;
  error: unknown;
}
const Ctx = createContext<MusicCtx>({ runtime: null, error: null });

/** Mount once, near the root (outside <Canvas>). Playback starts on the first user gesture. */
export function MusicProvider({ resolver, children, autoStart = true }: {
  resolver: UrlResolver;
  children: ReactNode;
  autoStart?: boolean;
}) {
  const [state, setState] = useState<MusicCtx>({ runtime: null, error: null });
  useEffect(() => installAudioUnlock(), []);
  useEffect(() => {
    let alive = true;
    initMusic(resolver).then(
      async (rt) => {
        if (!alive) return;
        setState({ runtime: rt, error: null });
        if (!autoStart || rt.director.current) return;
        // Wait for the context to be unlocked, then start the soundtrack.
        const go = () => rt.director.start().catch((error) => setState({ runtime: rt, error }));
        if (rt.ctx.state === 'running') go();
        else {
          const onState = () => {
            if (rt.ctx.state === 'running') {
              rt.ctx.removeEventListener('statechange', onState);
              go();
            }
          };
          rt.ctx.addEventListener('statechange', onState);
        }
      },
      (error) => alive && setState({ runtime: null, error }),
    );
    return () => {
      alive = false;
    };
    // resolver must be stable (define it at module level)
  }, [resolver, autoStart]);
  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

/** Imperative access for game code: const { trigger } = useMusic(); trigger('combat_start') */
export function useMusic() {
  const { runtime, error } = useContext(Ctx);
  const director = runtime?.director ?? null;
  const current = useCurrentTrack(director);
  return useMemo(
    () => ({
      ready: !!director,
      error,
      current,
      director,
      trigger: (name: string) => director?.trigger(name).catch((e) => console.error(e)) ?? Promise.resolve(null),
      setVolume: (v: number, fade = 0.5) => director?.setVolume(v, fade),
    }),
    [director, error, current],
  );
}

function useCurrentTrack(director: MusicDirector | null): string | null {
  return useSyncExternalStore(
    (cb) => (director ? director.on((e: DirectorEvent) => (e.type === 'track_started' || e.type === 'exit_scheduled') && cb()) : () => {}),
    () => director?.current ?? null,
  );
}

// ---------------------------------------------------------------- zones (R3F)

/** Stack of active zones: the most recently entered zone wins; leaving it falls back. */
const zoneStack: Array<{ id: symbol; target: string }> = [];
let zoneFallback: string | null = null;

function applyZones(director: MusicDirector | null) {
  const top = zoneStack[zoneStack.length - 1];
  const target = top?.target ?? zoneFallback;
  if (director && target) void director.trigger(target).catch(console.error);
}

/** Set what plays when the player is in no zone at all (e.g. 'enter_overworld'). */
export function setZoneFallback(eventOrTrack: string | null) {
  zoneFallback = eventOrTrack;
}

/**
 * Axis-aligned music volume. When `subject` (usually the player mesh) enters it,
 * `event` is triggered; when it leaves, the previous zone's event is re-triggered.
 * Put inside <Canvas>. Use `debug` to see the box.
 */
export function MusicZone({ event, subject, position = [0, 0, 0], size = [10, 10, 10], margin = 0.5, debug = false }: {
  event: string;
  subject: RefObject<THREE.Object3D | null>;
  position?: [number, number, number];
  size?: [number, number, number];
  margin?: number; // hysteresis so standing on the edge doesn't flicker
  debug?: boolean;
}) {
  const { director } = useMusic();
  const id = useRef(Symbol(event));
  const inside = useRef(false);
  const box = useMemo(() => {
    const half = new THREE.Vector3(...size).multiplyScalar(0.5);
    const c = new THREE.Vector3(...position);
    return new THREE.Box3(c.clone().sub(half), c.clone().add(half));
  }, [position, size]);
  const outer = useMemo(() => box.clone().expandByScalar(margin), [box, margin]);
  const p = useMemo(() => new THREE.Vector3(), []);

  useFrame(() => {
    const obj = subject.current;
    if (!obj) return;
    obj.getWorldPosition(p);
    if (!inside.current && box.containsPoint(p)) {
      inside.current = true;
      zoneStack.push({ id: id.current, target: event });
      applyZones(director);
    } else if (inside.current && !outer.containsPoint(p)) {
      inside.current = false;
      const i = zoneStack.findIndex((z) => z.id === id.current);
      if (i >= 0) zoneStack.splice(i, 1);
      applyZones(director);
    }
  });

  useEffect(() => () => {
    const i = zoneStack.findIndex((z) => z.id === id.current);
    if (i >= 0) zoneStack.splice(i, 1);
  }, []);

  return debug ? (
    <mesh position={position}>
      <boxGeometry args={size} />
      <meshBasicMaterial color="hotpink" wireframe />
    </mesh>
  ) : null;
}

/** Tiny DOM overlay for development: current track, phase, next exit times. */
export function MusicDebugOverlay() {
  const { director } = useMusic();
  const [text, setText] = useState('');
  useEffect(() => {
    if (!director) return;
    const t = setInterval(() => {
      const s = director.getState();
      const n = s.nextExit;
      setText(
        `${s.current ?? '-'}${s.pending ? ` → ${s.pending}` : ''} | ${s.phase}` +
          (s.loopPosition !== null ? ` @ ${s.loopPosition.toFixed(2)}s` : '') +
          (n ? ` | next beat ${n.beat.toFixed(2)} bar ${n.bar.toFixed(2)} phrase ${n.phrase.toFixed(2)}` : ''),
      );
    }, 100);
    return () => clearInterval(t);
  }, [director]);
  return (
    <div style={{ position: 'fixed', left: 8, bottom: 8, font: '12px monospace', color: '#0f0', background: '#000a', padding: '4px 8px', pointerEvents: 'none', zIndex: 9999 }}>
      ♪ {text}
    </div>
  );
}
