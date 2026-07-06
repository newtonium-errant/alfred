import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { RefObject } from 'react';
import { ApiError } from '../lib/algernon/http';
import { useVoice } from '../lib/algernon/useVoice';
import type { VoiceConfigResponse } from '../lib/algernon/types';

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

// --- Fakes ------------------------------------------------------------------

class FakeMediaStream {
  constructor(private tracks: FakeTrack[] = []) {}
  getTracks() {
    return this.tracks;
  }
  getAudioTracks() {
    return this.tracks;
  }
}

interface FakeTrack {
  kind: string;
  enabled: boolean;
  stop: ReturnType<typeof vi.fn>;
}

function makeTrack(): FakeTrack {
  return { kind: 'audio', enabled: true, stop: vi.fn() };
}

type Listener = () => void;

class FakeRTCPeerConnection {
  static instances: FakeRTCPeerConnection[] = [];
  static autoGather = true;

  config: RTCConfiguration;
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: unknown = null;
  iceGatheringState = 'new';
  connectionState = 'new';
  ontrack: ((ev: { streams: FakeMediaStream[]; track: unknown }) => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  closed = false;
  tracks: Array<{ track: FakeTrack; stream: FakeMediaStream }> = [];
  private listeners: Record<string, Listener[]> = {};

  constructor(config: RTCConfiguration) {
    this.config = config;
    FakeRTCPeerConnection.instances.push(this);
  }

  addTrack(track: FakeTrack, stream: FakeMediaStream) {
    this.tracks.push({ track, stream });
  }
  addEventListener(type: string, cb: Listener) {
    (this.listeners[type] ||= []).push(cb);
  }
  removeEventListener(type: string, cb: Listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== cb);
  }
  async createOffer() {
    return { type: 'offer', sdp: 'offer-sdp' };
  }
  async setLocalDescription(desc: { type: string; sdp?: string }) {
    this.localDescription = { type: desc.type, sdp: desc.sdp ?? 'offer-sdp' };
    this.iceGatheringState = FakeRTCPeerConnection.autoGather ? 'complete' : 'gathering';
  }
  async setRemoteDescription(desc: unknown) {
    this.remoteDescription = desc;
  }
  close() {
    this.closed = true;
    this.connectionState = 'closed';
  }

  // test drivers
  completeGathering() {
    this.iceGatheringState = 'complete';
    (this.listeners['icegatheringstatechange'] || []).forEach((f) => f());
  }
  emitConnectionState(s: string) {
    this.connectionState = s;
    this.onconnectionstatechange?.();
  }
  emitTrack(stream: FakeMediaStream) {
    this.ontrack?.({ streams: [stream], track: {} });
  }
}

// --- Harness ----------------------------------------------------------------

let lastMicTrack: FakeTrack;
let mockGetUserMedia: ReturnType<typeof vi.fn>;
let audioEl: { srcObject: unknown; play: ReturnType<typeof vi.fn> };
let audioRef: RefObject<HTMLAudioElement>;

function setPlay(resolves: boolean) {
  audioEl.play = resolves
    ? vi.fn().mockResolvedValue(undefined)
    : vi.fn().mockRejectedValue(new DOMException('blocked', 'NotAllowedError'));
}

beforeEach(() => {
  FakeRTCPeerConnection.instances = [];
  FakeRTCPeerConnection.autoGather = true;
  (global as unknown as { RTCPeerConnection: unknown }).RTCPeerConnection = FakeRTCPeerConnection;
  (global as unknown as { MediaStream: unknown }).MediaStream = FakeMediaStream;

  lastMicTrack = makeTrack();
  mockGetUserMedia = vi.fn().mockResolvedValue(new FakeMediaStream([lastMicTrack]));
  Object.defineProperty(global.navigator, 'mediaDevices', {
    value: { getUserMedia: mockGetUserMedia },
    configurable: true,
  });
  (global as unknown as { fetch: unknown }).fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });

  audioEl = { srcObject: null, play: vi.fn().mockResolvedValue(undefined) };
  audioRef = { current: audioEl } as unknown as RefObject<HTMLAudioElement>;

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

function lastPC(): FakeRTCPeerConnection {
  const pc = FakeRTCPeerConnection.instances.at(-1);
  if (!pc) throw new Error('no RTCPeerConnection was constructed');
  return pc;
}

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
    expect(mockOffer).toHaveBeenCalledWith('offer-sdp'); // localDescription.sdp
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

  it('goes to error connection-failed when the pc fails', async () => {
    const { result } = renderHook(() => useVoice({ audioRef, enabled: true }));
    await act(async () => {
      await result.current.start();
    });
    act(() => lastPC().emitConnectionState('connected'));
    expect(result.current.state).toBe('live');
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
    expect(result.current.state).toBe('idle');
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
