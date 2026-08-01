import { getJson } from './http';
import type { BriefNarration } from './player';

// BROWSER-side briefing-player client → the same-origin BFF (/api/brief/*), never the
// transport directly (the BFF holds the peer token + relays the session). Mirrors
// feedApi / the brief-page fetch shape.

// --- narration (slides source) ----------------------------------------------
// The narration fetch: the dict, or the backend's ILB no-brief marker (absent/corrupt
// spool → 200 {state:"no_brief"}). The page branches: noBrief → "no brief; here's the
// deck"; a dict → narrationSlides() (which itself returns [] for an empty:true dict —
// the distinct "brief exists, nothing speakable" ILB).
export type NarrationResult = { narration: BriefNarration } | { noBrief: true };

export async function fetchNarration(): Promise<NarrationResult> {
  const body = await getJson<BriefNarration | { state?: string }>('/api/brief/narration');
  if (body && typeof body === 'object' && (body as { state?: string }).state === 'no_brief') {
    return { noBrief: true };
  }
  return { narration: body as BriefNarration };
}

// --- audio (or its ILB / degradation state) ---------------------------------
// The audio route's outcome mapped to a player audio-state.
//
// FORWARD-COMPAT DEGRADATION (team-lead pin): any UNKNOWN / unrecognized JSON state
// string — OR a 502 / non-audio / network failure — degrades to `unavailable` (a
// generic honest "audio unavailable" + text-along mode), NEVER a crash or a broken play
// button. So the backend's pending third state renders safely under whatever name it
// ships as; naming it is a copy improvement, not a blocker. Same never-a-guess degrade
// family as the C2 render-present + acted_action-absent pins.
export type PlayerAudioState =
  | { kind: 'audio'; url: string }
  | { kind: 'no_brief' }
  | { kind: 'tts_not_configured' }
  | { kind: 'unavailable' };

export interface AudioProbe {
  ok: boolean;
  status: number;
  contentType: string;
  /** Parsed `state` when the body was JSON (else undefined). */
  state?: string;
  /** An object URL for the mp3 blob when the body was audio/mpeg (else undefined). */
  blobUrl?: string;
}

/** Pure: the audio-state from a probed audio response. See the forward-compat note above. */
export function playerAudioState(p: AudioProbe): PlayerAudioState {
  const ct = (p.contentType || '').toLowerCase();
  if (p.ok && ct.includes('audio/mpeg') && p.blobUrl) return { kind: 'audio', url: p.blobUrl };
  if (p.ok && ct.includes('application/json')) {
    if (p.state === 'no_brief') return { kind: 'no_brief' };
    if (p.state === 'tts_not_configured') return { kind: 'tts_not_configured' };
    return { kind: 'unavailable' }; // unknown / new state (incl. the pending third) → safe degrade
  }
  return { kind: 'unavailable' }; // 502 synth-fail / 401 / network / non-audio → text-along
}

/**
 * Fetch the day's briefing audio and resolve it to a PlayerAudioState. On an audio hit
 * it mints an object URL for `<audio src>` — the caller is responsible for revoking it
 * (URL.revokeObjectURL) when the player unmounts / re-fetches. Any failure degrades to
 * `unavailable` (text-along), never throws.
 */
export async function fetchAudio(speed?: number): Promise<PlayerAudioState> {
  const qs = typeof speed === 'number' && speed >= 0.7 && speed <= 1.2 ? `?speed=${speed}` : '';
  let res: Response;
  try {
    res = await fetch(`/api/brief/audio${qs}`, { credentials: 'same-origin' });
  } catch {
    return { kind: 'unavailable' }; // network → text-along, never a broken play button
  }
  const contentType = res.headers.get('content-type') || '';
  if (res.ok && contentType.toLowerCase().includes('audio/mpeg')) {
    const blob = await res.blob();
    return playerAudioState({ ok: true, status: res.status, contentType, blobUrl: URL.createObjectURL(blob) });
  }
  let state: string | undefined;
  try {
    const j = (await res.json()) as { state?: unknown };
    state = typeof j?.state === 'string' ? j.state : undefined;
  } catch {
    state = undefined;
  }
  return playerAudioState({ ok: res.ok, status: res.status, contentType, state });
}
