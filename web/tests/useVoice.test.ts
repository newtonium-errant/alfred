import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { RefObject } from 'react';
import { ApiError } from '../lib/algernon/http';
import { useVoice } from '../lib/algernon/useVoice';
import type { VoiceConfigResponse } from '../lib/algernon/types';
import {
  FakeMediaStream,
  FakeRTCPeerConnection,
  installVoiceGlobals,
  lastPC,
  makeTrack,
  type FakeTrack,
} from './helpers/webrtcFakes';

// Scripted WebRTC state machine test. A FakeRTCPeerConnection lets us drive the
// gathering / connection transitions deterministically; getUserMedia + voiceApi
// (config/offer) are mocked, while sendVoiceCloseBeacon stays REAL so the
// keepalive-close Content-Type (security W7) is asserted against a mocked fetch.

const { mockConfig, mockOffer } = vi.hoisted(() => ({
  mockConfig: vi.fn(),
  mockOffer: vi.fn(),
}));

vi.mock('../lib/algernon/voiceClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/algernon/voiceClient')>();
  return {
    ...actual, // keep the REAL sendVoiceCloseBeacon
    voiceApi: { config: mockConfig, offer: mockOffer, close: vi.fn() },
  };
});

// --- Harness (fakes shared via tests/helpers/webrtcFakes) --------------------

let lastMicTrack: FakeTrack;
let mockGetUserMedia: ReturnType<typeof vi.fn>;
let audioEl: { srcObject: unknown; play: ReturnType<typeof vi.fn> };
let audioRef: RefObject<HTMLAudioElement>;
let setPlay: (resolves: boolean) => void;

beforeEach(() => {
  const h = installVoiceGlobals();
  lastMicTrack = h.micTrack;
  mockGetUserMedia = h.getUserMedia;
  audioEl = h.audioEl;
  audioRef = h.audioRef as unknown as RefObject<HTMLAudioElement>;
  setPlay = h.setPlay;

  mockConfig.mockReset().mockResolvedValue({
    available: true,
    reason: null,
    ice_servers: [],
    max_sessions: 2,
    yours: [],
  });
  mockOffer.mockReset().mockResolvedValue({
    voice_session_id: 'vs-123',
    sdp: 'answer-sdp',
    type: 'answer',
    expires_at: 'z',
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useVoice', () => {
  it('runs idle → requesting-mic → connecting and POSTs the offer after gathering', async () => {
    // Non-empty ICE list so we can pin that config.ice_servers flows into the pc.
    const iceServers = [{ urls: ['stun:stun.example.net:3478'] }];
    mockConfig.mockResolvedValue({
      available: true,
      reason: null,
      ice_servers: iceServers,
      max_sessions: 2,
      yours: [],
    });
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(mockConfig).toHaveBeenCalledTimes(1);
    expect(mockGetUserMedia).toHaveBeenCalledTimes(1);
    expect(mockOffer).toHaveBeenCalledTimes(1);
    expect(mockOffer).toHaveBeenCalledWith('offer-sdp', undefined, undefined); // sdp + (no) instance + (no) sessionKey
    // The dictation datachannel is created BEFORE the offer so its SCTP m-section
    // rides the initial vanilla-ICE offer (no renegotiation).
    const ops = lastPC().ops;
    expect(ops.indexOf('createDataChannel:voice')).toBeGreaterThanOrEqual(0);
    expect(ops.indexOf('createDataChannel:voice')).toBeLessThan(ops.indexOf('createOffer'));
    // The server-provided ICE list is what the RTCPeerConnection was built with.
    expect((lastPC().config as { iceServers: unknown }).iceServers).toBe(iceServers);
    expect(result.current.state).toBe('connecting');
    expect(result.current.voiceSessionId).toBe('vs-123');

    // ontrack attaches the remote stream + plays it; connected → live.
    act(() => lastPC().emitTrack(new FakeMediaStream([makeTrack()])));
    expect(audioEl.play).toHaveBeenCalled();
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
  });

  it('waits for ICE gathering (event path) before POSTing the offer', async () => {
    FakeRTCPeerConnection.autoGather = false;
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    let startPromise: Promise<void> | undefined;
    await act(async () => {
      startPromise = result.current.start();
      await new Promise((r) => setTimeout(r, 0)); // flush the pre-gather chain
    });
    expect(mockOffer).not.toHaveBeenCalled();
    await act(async () => {
      lastPC().completeGathering();
      await startPromise;
    });
    expect(mockOffer).toHaveBeenCalledTimes(1);
  });

  it('falls back to the 3s gather guard when gathering never completes', async () => {
    vi.useFakeTimers();
    FakeRTCPeerConnection.autoGather = false;
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    let startPromise: Promise<void> | undefined;
    await act(async () => {
      startPromise = result.current.start();
      await vi.advanceTimersByTimeAsync(0); // flush the pre-gather chain
    });
    expect(mockOffer).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000); // fire the guard
      await startPromise;
    });
    expect(mockOffer).toHaveBeenCalledTimes(1);
  });

  it('errors voice-disabled from an unavailable config WITHOUT requesting the mic', async () => {
    mockConfig.mockResolvedValue({
      available: false,
      reason: 'aiortc_missing',
      ice_servers: [],
      max_sessions: 2,
      yours: [],
    });
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(mockGetUserMedia).not.toHaveBeenCalled();
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('voice-disabled');
  });

  it('maps a denied mic to permission-denied and never signals', async () => {
    mockGetUserMedia.mockRejectedValue(new DOMException('no', 'NotAllowedError'));
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('permission-denied');
    expect(mockOffer).not.toHaveBeenCalled();
  });

  it('maps a 429 offer to voice-busy and closes the pc', async () => {
    mockOffer.mockRejectedValue(new ApiError(429, 'too_many_sessions'));
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('voice-busy');
    expect(lastPC().closed).toBe(true);
  });

  it('maps a 401 offer to session-expired', async () => {
    mockOffer.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.error?.code).toBe('session-expired');
  });

  it('goes to error connection-failed when the pc fails before live (no pre-live auto-retry)', async () => {
    // A failure while still connecting surfaces the error immediately. A failure of a
    // LIVE session instead auto-retries once — that path is covered in
    // useVoiceReconnect.test.ts.
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('connecting');
    act(() => lastPC().emitConnectionState('failed'));
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('connection-failed');
  });

  it('toggleMute flips the mic track enabled flag without renegotiation', async () => {
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.muted).toBe(false);
    act(() => result.current.toggleMute());
    expect(lastMicTrack.enabled).toBe(false);
    expect(result.current.muted).toBe(true);
    act(() => result.current.toggleMute());
    expect(lastMicTrack.enabled).toBe(true);
    expect(result.current.muted).toBe(false);
    expect(lastPC().remoteDescription).toBeTruthy(); // no new negotiation object
  });

  it('hangup fires the keepalive close beacon with an application/json Blob and tears down', async () => {
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    act(() => lastPC().emitConnectionState('connected'));
    const pc = lastPC();
    act(() => result.current.hangup());

    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    const call = fetchMock.mock.calls.find((c) => c[0] === '/api/voice/close');
    expect(call).toBeTruthy();
    const init = call![1] as { method: string; keepalive?: boolean; body: Blob };
    expect(init.method).toBe('POST');
    expect(init.keepalive).toBe(true);
    expect(init.body).toBeInstanceOf(Blob);
    expect((init.body as Blob).type).toBe('application/json');

    expect(lastMicTrack.stop).toHaveBeenCalled();
    expect(pc.closed).toBe(true);
    // The datachannel is closed and its handlers nulled in teardownLocal.
    expect(pc.lastChannel().readyState).toBe('closed');
    expect(pc.lastChannel().onmessage).toBeNull();
    expect(result.current.state).toBe('idle');
  });

  it('binds the chat sessionKey into the offer (and start is gated by it in the UI)', async () => {
    const { result } = renderHook(() =>
      useVoice({ audioRef, enabled: true, sessionKey: 'sess-abc' }),
    );
    await act(async () => {
      await result.current.start();
    });
    expect(mockOffer).toHaveBeenCalledWith('offer-sdp', undefined, 'sess-abc');
  });

  it('fires the close beacon on unmount', async () => {
    const { result, unmount } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    (global.fetch as unknown as ReturnType<typeof vi.fn>).mockClear();
    unmount();
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/voice/close')).toBe(true);
    expect(lastMicTrack.stop).toHaveBeenCalled();
  });

  it('surfaces audio-blocked (recoverable) without tearing the call down', async () => {
    setPlay(false); // audio.play() rejects
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      lastPC().emitTrack(new FakeMediaStream([makeTrack()]));
      await Promise.resolve();
    });
    expect(result.current.audioBlocked).toBe(true);
    expect(result.current.state).toBe('connecting'); // pc still alive, NOT error
    expect(lastPC().closed).toBe(false);
  });

  it('errors unsupported without touching config or the mic', async () => {
    (global as unknown as { RTCPeerConnection: unknown }).RTCPeerConnection = undefined;
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error');
    expect(result.current.error?.code).toBe('unsupported');
    expect(mockConfig).not.toHaveBeenCalled();
    expect(mockGetUserMedia).not.toHaveBeenCalled();
  });

  it('does nothing when the hook is disabled', async () => {
    const { result } = renderHook(() => useVoice({ audioRef, enabled: false }));
    await act(async () => {
      await result.current.start();
    });
    expect(mockConfig).not.toHaveBeenCalled();
    expect(result.current.state).toBe('idle');
  });

  it('reset clears a terminal error back to idle', async () => {
    mockConfig.mockResolvedValue({
      available: false,
      reason: null,
      ice_servers: [],
      max_sessions: 2,
      yours: [],
    });
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state).toBe('error');
    act(() => result.current.reset());
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('aborts an in-flight start() on unmount — no mic acquired, no pc built after teardown', async () => {
    // Hold config pending so start() is suspended at `await voiceApi.config()`
    // when the component unmounts (the hot-mic leak window).
    let resolveConfig!: (v: VoiceConfigResponse) => void;
    mockConfig.mockReturnValue(new Promise<VoiceConfigResponse>((res) => (resolveConfig = res)));

    let startPromise: Promise<void> | undefined;
    const { result, unmount } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      startPromise = result.current.start(); // suspends at the pending config
    });
    expect(mockConfig).toHaveBeenCalledTimes(1);
    expect(mockGetUserMedia).not.toHaveBeenCalled();

    unmount(); // bumps genRef BEFORE teardown → the resumed start() must bail

    await act(async () => {
      resolveConfig({
        available: true,
        reason: null,
        ice_servers: [],
        max_sessions: 2,
        yours: [],
      });
      await startPromise;
    });

    // The stale guard must have fired: no mic acquired, no pc constructed.
    expect(mockGetUserMedia).not.toHaveBeenCalled();
    expect(FakeRTCPeerConnection.instances.length).toBe(0);
  });

  it('tears down a live call when enabled flips to false (instance switch)', async () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useVoice({ audioRef, enabled }),
      { initialProps: { enabled: true } },
    );
    await act(async () => {
      await result.current.start();
    });
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
    const pc = lastPC();
    (global.fetch as unknown as ReturnType<typeof vi.fn>).mockClear();

    act(() => rerender({ enabled: false }));

    expect(result.current.state).toBe('idle');
    expect(pc.closed).toBe(true);
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/voice/close')).toBe(true);
    expect(lastMicTrack.stop).toHaveBeenCalled();
  });
});
