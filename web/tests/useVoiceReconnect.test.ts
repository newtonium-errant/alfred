import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { RefObject } from 'react';
import { useVoice } from '../lib/algernon/useVoice';
import {
  FakeMediaStream,
  FakeRTCPeerConnection,
  installVoiceGlobals,
  lastPC,
  makeTrack,
  type FakeTrack,
  type VoiceGlobals,
} from './helpers/webrtcFakes';

// VOICE RECONNECT HARDENING — deterministic repros of the production wedge (a
// mid-live drop, then a reconnect that stuck forever at 'requesting-mic' holding a
// live mic) + the three fixes: clean reconnect, pre-live watchdog + bounded
// config/getUserMedia timeouts + exception-guarded pc build, and auto-retry-once.
// The mocks resolve instantly (fake providers), so the wedge is reproduced by
// SIMULATING the failure conditions (a hung promise, a construction throw, a drop).

const { mockConfig, mockOffer } = vi.hoisted(() => ({ mockConfig: vi.fn(), mockOffer: vi.fn() }));

vi.mock('../lib/algernon/voiceClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/algernon/voiceClient')>();
  return { ...actual, voiceApi: { config: mockConfig, offer: mockOffer, close: vi.fn() } };
});

let h: VoiceGlobals;
let audioRef: RefObject<HTMLAudioElement>;

const okConfig = { available: true, reason: null, ice_servers: [], max_sessions: 2, yours: [] };

beforeEach(() => {
  h = installVoiceGlobals();
  audioRef = h.audioRef as unknown as RefObject<HTMLAudioElement>;
  mockConfig.mockReset().mockResolvedValue(okConfig);
  mockOffer
    .mockReset()
    .mockResolvedValue({ voice_session_id: 'vs-1', sdp: 'answer-sdp', type: 'answer', expires_at: 'z' });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function render() {
  return renderHook(({ enabled }: { enabled: boolean }) => useVoice({ audioRef, enabled, sessionKey: 'k' }), {
    initialProps: { enabled: true },
  });
}

// Reach 'live' under fake timers (start() awaits are instant mocks; flush them).
async function goLiveFake(result: { current: ReturnType<typeof useVoice> }) {
  await act(async () => {
    const p = result.current.start();
    await vi.advanceTimersByTimeAsync(0);
    await p;
  });
  act(() => lastPC().emitConnectionState('connected'));
}

describe('useVoice reconnect hardening', () => {
  it('auto-retries ONCE on a transient live drop and reaches live again (old mic released, single active pc)', async () => {
    vi.useFakeTimers();
    const { result } = render();
    await goLiveFake(result);
    expect(result.current.state).toBe('live');
    const pc1 = lastPC();
    const mic1 = h.micTrack;

    // Mid-live drop: the pc fails unexpectedly.
    act(() => pc1.emitConnectionState('failed'));
    expect(result.current.reconnecting).toBe(true); // distinct "Reconnecting…" state
    expect(mic1.stop).toHaveBeenCalled(); // dead session's mic released — NEVER held
    expect(pc1.closed).toBe(true);
    expect(result.current.state).not.toBe('requesting-mic'); // no wedge

    // After the ~1.5s delay the auto-reconnect re-drives start() → connecting.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(FakeRTCPeerConnection.instances.length).toBe(2); // exactly one NEW pc
    expect(result.current.state).toBe('connecting');

    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
    expect(result.current.reconnecting).toBe(false);
    expect(pc1.closed).toBe(true); // old pc stays closed (no double-pc)
  });

  it('RE-ARMS the one-shot budget on a healthy reconnect (a SECOND consecutive live drop auto-retries AGAIN)', async () => {
    // The operator's repeated-drop scenario: the auto-retry budget is per live
    // session, re-armed when a reconnect actually reaches 'connected' — so a second
    // independent drop gets its own single retry (not exhausted by the first).
    vi.useFakeTimers();
    const { result } = render();
    await goLiveFake(result);
    expect(result.current.state).toBe('live');

    // Drop 1 → auto-retry → the reconnect reaches live (re-arms retriedRef on 'connected').
    act(() => lastPC().emitConnectionState('failed'));
    expect(result.current.reconnecting).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(FakeRTCPeerConnection.instances.length).toBe(2); // first reconnect pc
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
    expect(result.current.reconnecting).toBe(false);

    // Drop 2 on the now-healthy session: the budget must have RE-ARMED, so a SECOND
    // auto-retry genuinely fires (reconnecting again + a fresh reconnect attempt),
    // NOT a straight-to-error because the budget was still spent.
    act(() => lastPC().emitConnectionState('failed'));
    expect(result.current.reconnecting).toBe(true); // <-- the re-arm pin
    expect(result.current.state).not.toBe('error');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(FakeRTCPeerConnection.instances.length).toBe(3); // a THIRD pc — the second reconnect
    expect(result.current.state).toBe('connecting');
  });

  it('Cancel (hangup) during the ~1.5s retry gap aborts cleanly — mic released, retry cancelled, no reconnect fires', async () => {
    vi.useFakeTimers();
    const tracks: FakeTrack[] = [];
    h.getUserMedia.mockImplementation(() => {
      const t = makeTrack();
      tracks.push(t);
      return Promise.resolve(new FakeMediaStream([t]));
    });
    const { result } = render();
    await goLiveFake(result);
    expect(tracks.length).toBe(1); // the live session's mic

    act(() => lastPC().emitConnectionState('failed')); // drop → the ~1.5s retry gap
    expect(result.current.state).toBe('idle'); // transient — retry timer armed
    expect(result.current.reconnecting).toBe(true);
    expect(tracks[0].stop).toHaveBeenCalled(); // dead session's mic already released

    // Cancel mid-gap. hangup parks at 'idle' here, so this pins that hangup still
    // aborts a pending reconnect (cancels the retry timer) rather than no-op'ing.
    act(() => result.current.hangup());
    expect(result.current.state).toBe('idle');
    expect(result.current.reconnecting).toBe(false); // reconnect aborted

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000); // well past the retry delay
    });
    expect(FakeRTCPeerConnection.instances.length).toBe(1); // NO reconnect pc
    expect(tracks.length).toBe(1); // NO second mic acquired — retry timer was cleared
  });

  it('Cancel (hangup) during a stalled reconnect attempt aborts cleanly — reconnect mic released, lands idle re-armed', async () => {
    vi.useFakeTimers();
    const tracks: FakeTrack[] = [];
    h.getUserMedia.mockImplementation(() => {
      const t = makeTrack();
      tracks.push(t);
      return Promise.resolve(new FakeMediaStream([t]));
    });
    const { result } = render();
    await goLiveFake(result);
    act(() => lastPC().emitConnectionState('failed')); // drop → auto-retry
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toBe('connecting'); // reconnect in flight, never connects
    expect(result.current.reconnecting).toBe(true);
    expect(tracks.length).toBe(2); // the reconnect acquired a fresh mic
    const pc2 = lastPC();

    act(() => result.current.hangup()); // Cancel the stalled reconnect
    expect(result.current.state).toBe('idle');
    expect(result.current.reconnecting).toBe(false);
    expect(pc2.closed).toBe(true);
    expect(tracks[1].stop).toHaveBeenCalled(); // the reconnect's mic released

    // Re-armed: a subsequent manual start() works from the clean idle slate.
    await act(async () => {
      const p = result.current.start();
      await vi.advanceTimersByTimeAsync(0);
      await p;
    });
    expect(result.current.state).toBe('connecting');
    expect(tracks.length).toBe(3);
  });

  it('does NOT auto-retry on a user hangup', async () => {
    vi.useFakeTimers();
    const { result } = render();
    await goLiveFake(result);
    const pc1 = lastPC();
    act(() => result.current.hangup());
    expect(result.current.state).toBe('idle');
    // A stray drop on the torn-down pc must not resurrect anything.
    act(() => pc1.emitConnectionState('failed'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.reconnecting).toBe(false);
    expect(FakeRTCPeerConnection.instances.length).toBe(1); // no reconnect pc
  });

  it('does NOT auto-retry on an instance switch (enabled → false)', async () => {
    vi.useFakeTimers();
    const { result, rerender } = render();
    await goLiveFake(result);
    const pc1 = lastPC();
    act(() => rerender({ enabled: false })); // the enabled-effect tears the session down
    expect(result.current.state).toBe('idle');
    act(() => pc1.emitConnectionState('failed'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.reconnecting).toBe(false);
    expect(FakeRTCPeerConnection.instances.length).toBe(1);
  });

  it('the auto-retry is ONE-SHOT: a reconnect that also fails surfaces the error (no retry storm)', async () => {
    vi.useFakeTimers();
    const { result } = render();
    await goLiveFake(result);
    act(() => lastPC().emitConnectionState('failed')); // drop 1 → auto-retry
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toBe('connecting'); // the reconnect attempt
    // The reconnect attempt itself fails (during connecting) → clean error, NOT a
    // second auto-retry.
    act(() => lastPC().emitConnectionState('failed'));
    expect(result.current.state).toBe('error');
    expect(result.current.reconnecting).toBe(false);
  });

  it('a hung config is caught by its OWN bounded timeout (signaling-failed), never wedges at requesting-mic', async () => {
    vi.useFakeTimers();
    mockConfig.mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = render();
    await act(async () => {
      void result.current.start();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toBe('requesting-mic');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000); // the config timeout (< the 10s phase watchdog)
    });
    expect(result.current.state).toBe('error'); // no wedge
    expect(result.current.error?.code).toBe('signaling-failed');
    expect(h.getUserMedia).not.toHaveBeenCalled(); // config is first — mic never requested
  });

  it('requesting-mic WATCHDOG backstops a stalled phase (config ok, getUserMedia hangs) → reconnect-timeout', async () => {
    vi.useFakeTimers();
    // Config resolves UNDER its own 8s bound; getUserMedia then hangs. The per-await
    // gUM timeout would fire at 5s+8s=13s, but the phase watchdog (armed at 0, 10s)
    // is the backstop that fires first — clean error, never a wedge.
    mockConfig.mockImplementation(() => new Promise((r) => setTimeout(() => r(okConfig), 5000)));
    h.getUserMedia.mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = render();
    await act(async () => {
      void result.current.start();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state).toBe('requesting-mic');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000); // config resolves → gUM invoked (hangs)
    });
    expect(h.getUserMedia).toHaveBeenCalled();
    expect(result.current.state).toBe('requesting-mic'); // still pre-live
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000); // total 10s → the phase watchdog
    });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('reconnect-timeout');
  });

  it('a hung getUserMedia times out cleanly and a LATE mic grant is stopped (never leaked)', async () => {
    vi.useFakeTimers();
    let resolveGum!: (s: unknown) => void;
    h.getUserMedia.mockReturnValue(new Promise((r) => (resolveGum = r)));
    const { result } = render();
    await act(async () => {
      void result.current.start();
      await vi.advanceTimersByTimeAsync(0); // config resolves, gUM hangs
    });
    expect(result.current.state).toBe('requesting-mic');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000); // the getUserMedia timeout
    });
    expect(result.current.state).toBe('error'); // no wedge
    // A late mic grant (after the timeout) MUST be released, not leaked (hot-mic).
    const lateTrack = makeTrack();
    await act(async () => {
      resolveGum(new FakeMediaStream([lateTrack]));
      await Promise.resolve();
    });
    expect(lateTrack.stop).toHaveBeenCalled();
  });

  it('a pc-construction THROW releases the just-acquired mic and errors cleanly (the exact wedge repro)', async () => {
    FakeRTCPeerConnection.failNextConstruct = true;
    const { result } = render();
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error'); // NOT stuck at requesting-mic
    expect(result.current.error?.code).toBe('connection-failed');
    expect(h.micTrack.stop).toHaveBeenCalled(); // the just-acquired mic released — no hot mic
    expect(FakeRTCPeerConnection.instances.length).toBe(0); // construction threw before registering
  });

  it('reset() does a full clean teardown so a manual reconnect starts from a clean slate', async () => {
    vi.useFakeTimers();
    // Reach an error via a pc-build throw (leaves the machine in error).
    FakeRTCPeerConnection.failNextConstruct = true;
    const { result } = render();
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error');
    const micThatThrew = h.micTrack;
    expect(micThatThrew.stop).toHaveBeenCalled();

    // reset() → clean idle; a fresh start() then reaches live with a brand-new pc.
    act(() => result.current.reset());
    expect(result.current.state).toBe('idle');
    await goLiveFake(result);
    expect(result.current.state).toBe('live');
    expect(result.current.reconnecting).toBe(false);
    expect(FakeRTCPeerConnection.instances.length).toBe(1); // the throw registered none; the clean start made one
  });

  it('unmount mid-reconnect keeps the hot-mic pin green (mic stopped, retry cancelled)', async () => {
    vi.useFakeTimers();
    const { result, unmount } = render();
    await goLiveFake(result);
    const mic1 = h.micTrack;
    act(() => lastPC().emitConnectionState('failed')); // drop → auto-retry scheduled
    expect(result.current.reconnecting).toBe(true);
    unmount(); // must cancel the pending retry + release the mic
    expect(mic1.stop).toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000); // the retry timer must NOT fire a new start
    });
    expect(FakeRTCPeerConnection.instances.length).toBe(1); // no reconnect pc after unmount
  });
});
