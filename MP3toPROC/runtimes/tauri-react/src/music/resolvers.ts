// How the webview finds the music files. Pick ONE strategy per project (see playbook §5).
import type { UrlResolver } from './library';

/** Strategy A (default): assets live in `public/music/`, Vite copies them into the
 *  frontend bundle, and Tauri embeds that bundle. Works the same in `tauri dev` and release. */
export const publicFolderResolver =
  (root = '/music/'): UrlResolver =>
  (rel) =>
    root + rel;

/** Strategy B: assets shipped as Tauri bundle resources (`src-tauri/music/**`), served
 *  through the asset protocol. Use for big soundtracks or DLC packs. */
export const tauriResourceResolver = (root = 'music/'): UrlResolver => {
  const cache = new Map<string, string>();
  return async (rel) => {
    const hit = cache.get(rel);
    if (hit) return hit;
    const { convertFileSrc } = await import('@tauri-apps/api/core');
    const { resolveResource } = await import('@tauri-apps/api/path');
    const url = convertFileSrc(await resolveResource(root + rel));
    cache.set(rel, url);
    return url;
  };
};

/** True when running inside Tauri (vs `vite` in a normal browser tab). */
export const isTauri = (): boolean => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
