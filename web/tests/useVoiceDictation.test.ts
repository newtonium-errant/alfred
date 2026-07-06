import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { RefObject } from 'react';
import { useVoice } from '../lib/algernon/useVoice';
import {
  installVoiceGlobals,
  lastPC,
  type FakeDataChannel,
  type VoiceGlobals,
} from './helpers/webrtcFakes';

// V1 dictation datachannel flow — canonical D2 wire vocabulary. Drives the fake
// datachannel through the streaming-STT → text-reply lifecycle and pins the
// sub-state machine, thread adoption, error taxonomy, and the V0 hot-mic invariant.

const { mockConfig, mockOffer } = vi.hoisted(() => ({ mockConfig: vi.fn(), mockOffer: vi.fn() }));

vi.mock('../lib/algernon/voiceClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/algernon/voiceClient')>();
  return { ...actual, voiceApi: { config: mockConfig, offer: mockOffer, close: vi.fn() } };
});

let h: VoiceGlobals;
let audioRef: RefObject<HTMLAudioElement>;

beforeEach(() => {
  h = installVoiceGlobals();
  audioRef = h.audioRef as unknown as RefObject<HTMLAudioElement>;
  mockConfig.mockReset().mockResolvedValue({
    available: true,
    reason: null,
    ice_servers: [],
    max_sessions: 2,
    yours: [],
  });
  mockOffer.mockReset().mockResolvedValue({
    voice_session_id: 'vs-1',
    sdp: 'answer-sdp',
    type: 'answer',
    expires_at: 'z',
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// Canonical frame (every frame carries v:1).
function f(obj: Record<string, unknown>) {
  return { v: 1, ...obj };
}

async function goLive(opts: { onTurnFinal?: () => Promise<boolean> } = {}) {
  const rendered = renderHook(() =>
    useVoice({ audioRef, enabled: true, sessionKey: 'sess-1', onTurnFinal: opts.onTurnFinal }),
  );
  await act(async () => {
    await rendered.result.current.start();
  });
  act(() => lastPC().emitConnectionState('connected'));
  const dc = lastPC().lastChannel();
  return { ...rendered, dc };
}

const emit = (dc: FakeDataChannel, obj: Record<string, unknown>) =>
  act(() => dc.emitMessage(f(obj)));

function activateDictation(dc: FakeDataChannel) {
  act(() => dc.emitOpen());
  emit(dc, { type: 'state', state: 'ready', chat_session_key: 'ck', voice_session_id: 'vs-1' });
}

describe('useVoice dictation datachannel', () => {
  it('sends the v:1 hello frame when the channel opens', async () => {
    const { dc } = await goLive();
    act(() => dc.emitOpen());
    expect(dc.sent).toHaveLength(1);
    expect(JSON.parse(dc.sent[0])).toEqual({ v: 1, type: 'hello' });
  });

  it('ready activates dictation; a fresh utterance → thinking → replying → final adopts the thread', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await goLive({ onTurnFinal });
    activateDictation(dc);
    expect(result.current.dictationUnavailable).toBe(false);
    expect(result.current.voiceTurnState).toBe('listening');

    emit(dc, { type: 'stt_partial', utterance_id: 'u1', text: 'what is' });
    expect(result.current.partialTranscript).toBe('what is');
    emit(dc, { type: 'stt_final', utterance_id: 'u1', text: 'what is on my calendar' });
    expect(result.current.partialTranscript).toBe('what is on my calendar');
    expect(result.current.voiceTurnState).toBe('thinking');

    emit(dc, { type: 'turn_started', turn_id: 't1' });
    expect(result.current.voiceTurnState).toBe('thinking');
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'You have ' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 1, text: 'two meetings.' });
    expect(result.current.voiceTurnState).toBe('replying');
    expect(result.current.replyText).toBe('You have two meetings.');

    await act(async () => {
      dc.emitMessage(
        f({ type: 'turn_final', turn_id: 't1', reply: 'You have two meetings.', ts: 'a', user_ts: 'b' }),
      );
      await Promise.resolve();
    });
    expect(onTurnFinal).toHaveBeenCalledTimes(1);
    expect(result.current.voiceTurnState).toBe('listening');
    // Adopted (onTurnFinal → true) ⇒ the in-panel copy is cleared (now in the thread).
    expect(result.current.replyText).toBe('');
    expect(result.current.partialTranscript).toBe('');
  });

  it('keeps the reply in-panel when thread adoption returns false', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(false);
    const { result, dc } = await goLive({ onTurnFinal });
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'Hello there.' });
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'Hello there.' }));
      await Promise.resolve();
    });
    expect(result.current.replyText).toBe('Hello there.'); // retained (graceful)
  });

  it('a stale onTurnFinal resolution cannot clobber a fresh session (isolates the reconcile gen-guard)', async () => {
    // Turn 1's onTurnFinal is held PENDING so it resolves only AFTER teardown —
    // this isolates the generation guard at the async reconcile continuation
    // (handler-nulling doesn't help: the .then is already scheduled).
    let resolveAdopt!: (v: boolean) => void;
    const onTurnFinal = vi
      .fn()
      .mockImplementation(() => new Promise<boolean>((r) => (resolveAdopt = r)));
    const { result, dc } = await goLive({ onTurnFinal });
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'first reply' });
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'first reply' }));
      await Promise.resolve();
    });
    expect(onTurnFinal).toHaveBeenCalledTimes(1); // called, but its promise is pending

    // Tear the call down (bumps genRef → the pending .then's captured gen is stale).
    act(() => result.current.hangup());

    // Open a SECOND session and stream a new reply into the panel.
    await act(async () => {
      await result.current.start();
    });
    act(() => lastPC().emitConnectionState('connected'));
    const dc2 = lastPC().lastChannel();
    activateDictation(dc2);
    emit(dc2, { type: 'turn_started', turn_id: 't2' });
    emit(dc2, { type: 'turn_text', turn_id: 't2', seq: 0, text: 'second reply' });
    expect(result.current.replyText).toBe('second reply');

    // Turn 1's onTurnFinal FINALLY resolves — the gen guard must stop its
    // continuation from clearing the SECOND session's reply.
    await act(async () => {
      resolveAdopt(true);
      await Promise.resolve();
    });
    expect(result.current.replyText).toBe('second reply'); // NOT clobbered by the stale continuation
  });

  it('turn_tool surfaces the tool name during the turn', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_tool', turn_id: 't1', tool: 'vault_search' });
    expect(result.current.toolName).toBe('vault_search');
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'Found it.' });
    expect(result.current.toolName).toBeNull(); // cleared when the reply starts
  });

  it('state:superseded drops the abandoned reply and shows thinking', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'old reply' });
    expect(result.current.replyText).toBe('old reply');
    emit(dc, { type: 'state', state: 'superseded', turn_id: 't1' });
    expect(result.current.replyText).toBe('');
    expect(result.current.voiceTurnState).toBe('thinking');
  });

  it('state:turn_cancelled returns to listening', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'partial' });
    emit(dc, { type: 'state', state: 'turn_cancelled', turn_id: 't1' });
    expect(result.current.voiceTurnState).toBe('listening');
    expect(result.current.replyText).toBe('');
  });

  it('cancelTurn sends a v:1 cancel frame for the active turn', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    dc.sent.length = 0; // ignore the hello
    act(() => result.current.cancelTurn());
    expect(dc.sent).toHaveLength(1);
    expect(JSON.parse(dc.sent[0])).toEqual({ v: 1, type: 'cancel', turn_id: 't1' });
  });

  it('error{stt_unavailable} is fatal → stt-failed + mic stopped', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'error', code: 'stt_unavailable', detail: 'provider down' });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('stt-failed');
    expect(h.micTrack.stop).toHaveBeenCalled();
  });

  it('a non-fatal error{code} sets a turn notice but keeps the call live', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'error', code: 'no_such_session', detail: 'gone', turn_id: 't1' });
    expect(result.current.state).toBe('live');
    expect(result.current.turnError).toBe('gone');
    expect(result.current.voiceTurnState).toBe('listening');
  });

  it('an unexpected channel death AFTER ready is fatal → channel-failed', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    act(() => dc.emitClose());
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('channel-failed');
    expect(h.micTrack.stop).toHaveBeenCalled();
  });

  it('a channel close BEFORE ready (echo pipeline) is benign — the session stays live', async () => {
    const { result, dc } = await goLive();
    // No ready emitted → dictation never activated.
    act(() => dc.emitClose());
    expect(result.current.state).toBe('live');
    expect(result.current.error).toBeNull();
  });

  it('echo/absent pipeline: no ready within the watchdog → a NON-fatal notice', async () => {
    vi.useFakeTimers();
    // Default config has no `pipeline` field ⇒ the benign path.
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true, sessionKey: 'sess-1' }));
    await act(async () => {
      const p = result.current.start();
      await vi.advanceTimersByTimeAsync(0);
      await p;
    });
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
    expect(result.current.dictationUnavailable).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(result.current.dictationUnavailable).toBe(true);
    expect(result.current.state).toBe('live'); // NOT a hard fail
  });

  it('assistant pipeline: no ready within the watchdog is FATAL (channel-failed)', async () => {
    mockConfig.mockResolvedValue({
      available: true,
      reason: null,
      ice_servers: [],
      max_sessions: 2,
      yours: [],
      pipeline: 'assistant',
    });
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true, sessionKey: 'sess-1' }));
    await act(async () => {
      const p = result.current.start();
      await vi.advanceTimersByTimeAsync(0);
      await p;
    });
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('channel-failed');
    expect(h.micTrack.stop).toHaveBeenCalled(); // hot mic released
  });

  it('a channel close during our own hangup does not error', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    act(() => result.current.hangup());
    act(() => dc.emitClose()); // handlers were nulled in teardown
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('drops unknown-type and malformed frames without any state change', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'tts_started', text: 'future v2 frame' }); // unknown type
    act(() => dc.emitRaw('{ not valid json')); // malformed
    act(() => dc.emitRaw(123)); // non-string
    expect(result.current.state).toBe('live');
    expect(result.current.voiceTurnState).toBe('listening');
    expect(debug).toHaveBeenCalled();
  });

  it('a stale frame after teardown is ignored (gen guard)', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    act(() => result.current.hangup()); // bumps genRef, nulls dc handlers
    // Even a raw re-dispatch cannot resurrect state.
    act(() => dc.emitMessage(f({ type: 'turn_text', turn_id: 't1', seq: 0, text: 'zombie' })));
    expect(result.current.state).toBe('idle');
    expect(result.current.replyText).toBe('');
  });

  it('unmount mid-turn keeps the hot-mic pin green (mic stopped, dc closed)', async () => {
    const { dc, unmount } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'mid' });
    unmount();
    expect(h.micTrack.stop).toHaveBeenCalled();
    expect(dc.readyState).toBe('closed');
  });
});

describe('useVoice speaking lifecycle (V2 TTS talk-back)', () => {
  // Reach 'speaking': live → dictation ready → a turn whose reply is streaming AND
  // whose TTS playout has started.
  async function goSpeaking(opts: { onTurnFinal?: () => Promise<boolean> } = {}) {
    const rendered = await goLive(opts);
    const { dc } = rendered;
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'You have ' });
    emit(dc, { type: 'speaking_started', turn_id: 't1' });
    return rendered;
  }

  it('speaking_started → speaking; turn_text WHILE speaking keeps speaking + accumulates', async () => {
    const { result, dc } = await goSpeaking();
    expect(result.current.voiceTurnState).toBe('speaking');
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 1, text: 'two meetings.' });
    expect(result.current.voiceTurnState).toBe('speaking'); // no ping-pong back to replying
    expect(result.current.replyText).toBe('You have two meetings.');
  });

  it('turn_final while speaking stays speaking; onTurnFinal fires + reply adopts', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await goSpeaking({ onTurnFinal });
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'You have two meetings.' }));
      await Promise.resolve();
    });
    expect(onTurnFinal).toHaveBeenCalledTimes(1);
    expect(result.current.voiceTurnState).toBe('speaking'); // audio still playing
    expect(result.current.replyText).toBe(''); // adopted into the thread
    expect(result.current.canCancel).toBe(false);
  });

  it('speaking_done after turn_final → listening', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await goSpeaking({ onTurnFinal });
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'r' }));
      await Promise.resolve();
    });
    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'drained' });
    expect(result.current.voiceTurnState).toBe('listening');
  });

  it('speaking_done BEFORE turn_final → replying, then turn_final → listening', async () => {
    const { result, dc } = await goSpeaking();
    emit(dc, { type: 'speaking_done', turn_id: 't1' });
    expect(result.current.voiceTurnState).toBe('replying'); // text still in flight
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'r' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('listening');
  });

  it('a turn with NO speaking events → turn_final → listening (V1 byte-identical pin)', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await goLive({ onTurnFinal });
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'text reply' });
    expect(result.current.voiceTurnState).toBe('replying');
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'text reply' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('listening');
  });

  it('speaking_started arriving AFTER turn_final → speaking → listening', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await goLive({ onTurnFinal });
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'short' });
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'short' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('listening');
    emit(dc, { type: 'speaking_started', turn_id: 't1' }); // audio starts after text done
    expect(result.current.voiceTurnState).toBe('speaking');
    emit(dc, { type: 'speaking_done', turn_id: 't1' });
    expect(result.current.voiceTurnState).toBe('listening');
  });

  it('turn_cancelled mid-speaking → listening; a stray speaking_done is a no-op', async () => {
    const { result, dc } = await goSpeaking();
    emit(dc, { type: 'state', state: 'turn_cancelled', turn_id: 't1' });
    expect(result.current.voiceTurnState).toBe('listening');
    expect(result.current.replyText).toBe('');
    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'cancelled' }); // stale/dup
    expect(result.current.voiceTurnState).toBe('listening'); // idempotent, unchanged
  });

  it('error{tts_unavailable} is non-destructive (its OWN branch, not the generic one)', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'streaming reply' });
    expect(result.current.voiceTurnState).toBe('replying');
    emit(dc, { type: 'error', code: 'tts_unavailable', detail: 'tier' });
    expect(result.current.ttsUnavailable).toBe(true);
    expect(result.current.state).toBe('live');
    expect(result.current.replyText).toBe('streaming reply'); // NOT cleared
    expect(result.current.voiceTurnState).toBe('replying'); // unchanged
    expect(result.current.turnError).toBeNull();
  });

  it('speaking_started clears a prior tts-unavailable notice (self-heal)', async () => {
    const { result, dc } = await goLive();
    activateDictation(dc);
    emit(dc, { type: 'error', code: 'tts_unavailable' });
    expect(result.current.ttsUnavailable).toBe(true);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'speaking_started', turn_id: 't1' });
    expect(result.current.ttsUnavailable).toBe(false);
  });

  it('utterance_discarded shows the honest notice; cleared when speaking ends', async () => {
    const { result, dc } = await goSpeaking();
    emit(dc, { type: 'utterance_discarded', utterance_id: 'u9' });
    expect(result.current.discardNotice).toBe(true);
    emit(dc, { type: 'speaking_done', turn_id: 't1' });
    expect(result.current.discardNotice).toBe(false);
  });

  it('toggleSpeakerMute flips the audio element .muted (client-local)', async () => {
    const { result } = await goSpeaking();
    expect(h.audioEl.muted).toBe(false);
    act(() => result.current.toggleSpeakerMute());
    expect(h.audioEl.muted).toBe(true);
    expect(result.current.speakerMuted).toBe(true);
    act(() => result.current.toggleSpeakerMute());
    expect(h.audioEl.muted).toBe(false);
    expect(result.current.speakerMuted).toBe(false);
  });

  it('hangup resets speaker mute AND unmutes the audio element (and no-ops when idle)', async () => {
    const { result } = await goSpeaking();
    act(() => result.current.toggleSpeakerMute());
    expect(h.audioEl.muted).toBe(true);
    act(() => result.current.hangup());
    expect(result.current.speakerMuted).toBe(false);
    expect(h.audioEl.muted).toBe(false);
    expect(result.current.state).toBe('idle');
    act(() => result.current.toggleSpeakerMute()); // idle → no-op
    expect(h.audioEl.muted).toBe(false);
  });

  it('canCancel gates the cancel frame: sent during the turn, silent post-final', async () => {
    const { result, dc } = await goSpeaking();
    expect(result.current.canCancel).toBe(true);
    dc.sent.length = 0;
    act(() => result.current.cancelTurn());
    expect(dc.sent).toHaveLength(1);
    expect(JSON.parse(dc.sent[0])).toEqual({ v: 1, type: 'cancel', turn_id: 't1' });
    // Post-final playout: the turn id is cleared → cancel is a no-op (honest control).
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'r' }));
      await Promise.resolve();
    });
    expect(result.current.canCancel).toBe(false);
    dc.sent.length = 0;
    act(() => result.current.cancelTurn());
    expect(dc.sent).toHaveLength(0);
  });

  it('unmount mid-speaking keeps the hot-mic pin green', async () => {
    const { dc, unmount } = await goSpeaking();
    unmount();
    expect(h.micTrack.stop).toHaveBeenCalled();
    expect(dc.readyState).toBe('closed');
  });
});

describe('useVoice V3 barge-in event orderings (FE frozen; ratified §1.6 table)', () => {
  // Reach 'speaking' for turn t1 (currentTurnIdRef=t1, speakingTurnIdRef=t1) — the
  // PRE-FINAL state. onTurnFinal adopts so replyText clears into the thread.
  async function reachSpeaking(onTurnFinal?: () => Promise<boolean>) {
    const rendered = await goLive({ onTurnFinal });
    const { dc } = rendered;
    activateDictation(dc);
    emit(dc, { type: 'turn_started', turn_id: 't1' });
    emit(dc, { type: 'turn_text', turn_id: 't1', seq: 0, text: 'You have ' });
    emit(dc, { type: 'speaking_started', turn_id: 't1' });
    return rendered;
  }

  it('POST-FINAL × CONFIRM: speaking → thinking → (listening blip) → new turn; no discard notice', async () => {
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await reachSpeaking(onTurnFinal);
    // t1 finishes its text → post-final playout (still 'speaking', reply adopted).
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'orig reply' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('speaking');
    expect(result.current.replyText).toBe('');

    // Barge: the user speaks over the drain (confirmed).
    emit(dc, { type: 'stt_partial', utterance_id: 'u2', text: 'actually wait' });
    expect(result.current.partialTranscript).toBe('actually wait');
    expect(result.current.voiceTurnState).toBe('speaking'); // partials never touch the pill
    expect(result.current.discardNotice).toBe(false);

    emit(dc, { type: 'stt_final', utterance_id: 'u2', text: 'actually never mind that' });
    expect(result.current.voiceTurnState).toBe('thinking'); // barge registered

    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'barged_in' });
    expect(result.current.voiceTurnState).toBe('listening'); // honest rescue blip (currentTurnId null)
    expect(result.current.discardNotice).toBe(false);

    emit(dc, { type: 'turn_started', turn_id: 't2' });
    expect(result.current.voiceTurnState).toBe('thinking');
    expect(result.current.canCancel).toBe(true);
    emit(dc, { type: 'turn_text', turn_id: 't2', seq: 0, text: 'The new answer.' });
    expect(result.current.voiceTurnState).toBe('replying');
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't2', reply: 'The new answer.' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('listening');
    // discardNotice never fired anywhere on the confirm path.
    expect(result.current.discardNotice).toBe(false);
  });

  it('PRE-FINAL × CONFIRM: speaking_done blips replying, turn_cancelled clears the old reply', async () => {
    const { result, dc } = await reachSpeaking();
    expect(result.current.replyText).toBe('You have '); // t1 still streaming

    emit(dc, { type: 'stt_partial', utterance_id: 'u2', text: 'no wait' });
    expect(result.current.replyText).toBe('You have '); // NOT cleared (currentTurnId non-null)
    expect(result.current.partialTranscript).toBe('no wait');

    emit(dc, { type: 'stt_final', utterance_id: 'u2', text: 'no wait stop' });
    expect(result.current.voiceTurnState).toBe('thinking');

    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'barged_in' });
    expect(result.current.voiceTurnState).toBe('replying'); // honest blip (currentTurnId still t1)

    emit(dc, { type: 'state', state: 'turn_cancelled', turn_id: 't1' });
    expect(result.current.replyText).toBe(''); // old turn's reply cleared
    expect(result.current.canCancel).toBe(false);
    expect(result.current.voiceTurnState).toBe('listening');
    expect(result.current.partialTranscript).toBe('no wait stop'); // barging input survives

    emit(dc, { type: 'turn_started', turn_id: 't2' });
    expect(result.current.voiceTurnState).toBe('thinking');
    emit(dc, { type: 'turn_text', turn_id: 't2', seq: 0, text: 'Okay.' });
    expect(result.current.replyText).toBe('Okay.'); // fresh accumulation
  });

  it('a duplicate speaking_done{barged_in} is a no-op (idempotency at the null-check)', async () => {
    const { result, dc } = await reachSpeaking();
    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'barged_in' });
    expect(result.current.voiceTurnState).toBe('replying'); // currentTurnId t1 (pre-final)
    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'barged_in' }); // dup
    expect(result.current.voiceTurnState).toBe('replying'); // unchanged (speakingTurnIdRef already null)
  });

  it('barge-DISABLED (V2 discard) rescue pin: stt_final→thinking, discard→notice, speaking_done{drained}→listening', async () => {
    // The LOAD-BEARING transition the zero-source decision rests on: speaking_done
    // is the ONLY thing that rescues the pill from the 'thinking' a discarded final
    // leaves behind. A preserve-'thinking' guard here would wedge V2 forever.
    const onTurnFinal = vi.fn().mockResolvedValue(true);
    const { result, dc } = await reachSpeaking(onTurnFinal);
    await act(async () => {
      dc.emitMessage(f({ type: 'turn_final', turn_id: 't1', reply: 'r' }));
      await Promise.resolve();
    });
    expect(result.current.voiceTurnState).toBe('speaking'); // post-final drain

    emit(dc, { type: 'stt_final', utterance_id: 'u2', text: 'mm hmm' }); // final surfaced (honesty)
    expect(result.current.voiceTurnState).toBe('thinking'); // the trap seed
    emit(dc, { type: 'utterance_discarded', utterance_id: 'u2' });
    expect(result.current.discardNotice).toBe(true);
    expect(result.current.voiceTurnState).toBe('thinking'); // discard does NOT rescue

    emit(dc, { type: 'speaking_done', turn_id: 't1', reason: 'drained' });
    expect(result.current.voiceTurnState).toBe('listening'); // RESCUED (currentTurnId null)
    expect(result.current.discardNotice).toBe(false);
  });
});
