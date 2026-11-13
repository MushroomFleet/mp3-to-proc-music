// Schema of the files produced by the MP3toPROC converter (mp3toproc_version 1).
// Do not edit these files by hand in the game repo; regenerate them with the pipeline.

export interface SegmentInfo {
  file: string | null; // relative to the track folder; null = segment is empty
  samples: number; // at manifest.sample_rate
  seconds: number;
}

export interface TrackManifest {
  mp3toproc_version: number;
  id: string;
  source: string;
  sample_rate: number;
  channels: number;
  bpm: number;
  beats_per_bar: number;
  segments: {
    intro: SegmentInfo;
    loop: SegmentInfo & {
      bars: number;
      bar_offsets: number[]; // samples from loop start
      phrase_offsets: number[];
      beat_offsets: number[];
    };
    outro: SegmentInfo & { tail_offset?: number };
  };
  transition_defaults?: { crossfade_seconds?: number; quantize?: Quantize };
  analysis: {
    loop_score: number;
    confidence: 'high' | 'medium' | 'low';
    warnings: string[];
    [k: string]: unknown;
  };
}

export type Quantize = 'immediate' | 'beat' | 'bar' | 'phrase' | 'loop_end';
export type Mode = 'outro' | 'cut';

export interface TransitionRule {
  mode: Mode;
  quantize: Quantize;
  crossfade_seconds: number;
  outro_max_seconds: number | null;
  outro_from: 'start' | 'tail';
  skip_intro: boolean;
  seam_seconds: number;
  lead_seconds: number;
}

export type TrackRef = string | { manifest: string; gain?: number };
export type EventTarget = string | (Partial<TransitionRule> & { target: string });

export interface SoundtrackConfig {
  sample_rate?: number;
  start?: string;
  tracks: Record<string, TrackRef>;
  defaults?: Partial<TransitionRule>;
  transitions?: Array<Partial<TransitionRule> & { from?: string; to?: string; _comment?: string }>;
  events?: Record<string, EventTarget>;
}

export const DEFAULT_RULE: TransitionRule = {
  mode: 'outro',
  quantize: 'phrase',
  crossfade_seconds: 4,
  outro_max_seconds: null,
  outro_from: 'start',
  skip_intro: false,
  seam_seconds: 0.12,
  lead_seconds: 0.05,
};

export const SUPPORTED_MANIFEST_VERSION = 1;
