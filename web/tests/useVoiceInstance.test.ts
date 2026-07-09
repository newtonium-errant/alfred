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

// MULTI-INSTANCE VOICE — the FE instance-threading correctness: the offer binds to
// the selected instance, and an instance SWITCH mid-live-call tears the old session
// down via the DEDICATED instance-change effect (NOT the enabled-flag effect, which
// no longer fires when both instances are voice-capable), routing the close beacon
// through the session's OWN (offer-time) instance so the old backend — not the newly
// selected one — receives the close.

const { mockConfig, mockOffer, mockClose, mockBeacon } = vi.hoisted(() => ({
  mockConfig: vi.fn(),
  mockOffer: vi.fn(),
  mockClose: vi.fn(),
  mockBeacon: vi.fn(),
}));

vi.mock('../lib/algernon/voiceClient', () => ({
  voiceApi: { config: mockConfig, offer: mockOffer, close: mockClose },
  sendVoiceCloseBeacon: mockBeacon,
}));

let h: VoiceGlobals;
let audioRef: RefObject<HTMLAudioElement>;

const okConfig = { available: true, reason: null, ice_servers: [], max_sessions: 2, yours: [] };

beforeEach(() => {
  h = installVoiceGlobals();
  audioRef = h.audioRef as unknown as RefObject<HTMLAudioElement>;
  mockConfig.mockReset().mockResolvedValue(okConfig);
  mockOffer.mockReset().mockResolvedValue({
    voice_session_id: 'vs-1',
    sdp: 'answer-sdp',
    type: 'answer',
    expires_at: 'z',
  });
  mockClose.mockReset().mockResolvedValue({ closed: true });
  mockBeacon.mockReset();
  // Distinct mic track per getUserMedia call so a switch's mic hygiene is observable.
  h.getUserMedia.mockImplementation(() => Promise.resolve(new FakeMediaStream([makeTrack()])));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function renderVoice(instance: string) {
  return renderHook(
    ({ instance }: { instance: string }) =>
      useVoice({ audioRef, enabled: true, instance, sessionKey: 'k' }),
    { initialProps: { instance } },
  );
}

async function goLive(result: { current: ReturnType<typeof useVoice> }) {
  await act(async () => {
    await result.current.start();
  });
  act(() => lastPC().emitConnectionState('connected'));
}

describe('useVoice multi-instance threading', () => {
  it('binds the offer + config to the selected instance', async () => {
    const { result } = renderVoice('HYPATIA');
    await goLive(result);
    expect(result.current.state).toBe('live');
    expect(mockConfig).toHaveBeenCalledWith('HYPATIA');
    // voiceApi.offer(sdp, instance, sessionKey)
    expect(mockOffer).toHaveBeenCalledWith('offer-sdp', 'HYPATIA', 'k');
  });

  it('an instance switch mid-live-call tears the OLD session down (dedicated effect; enabled stays true)', async () => {
    const { result, rerender } = renderVoice('SALEM');
    await goLive(result);
    const pc1 = lastPC();
    const mic1 = h.micTrack; // the first session's mic tracked separately below
    expect(result.current.state).toBe('live');

    // Switch to a DIFFERENT voice instance. `enabled` stays true (both voice-capable),
    // so the enabled-flag effect does NOT fire — the dedicated instance-change effect
    // is what hangs the old call up.
    act(() => rerender({ instance: 'HYPATIA' }));

    expect(result.current.state).toBe('idle'); // old session torn down
    expect(pc1.closed).toBe(true);
    // No second pc was built by the switch itself (start is a user gesture).
    expect(FakeRTCPeerConnection.instances.length).toBe(1);
    void mic1;
  });

  it('the close on switch routes to the OLD (offer-time) instance, NOT the newly-selected one', async () => {
    const { result, rerender } = renderVoice('SALEM');
    await goLive(result);
    expect(result.current.voiceSessionId).toBe('vs-1');

    act(() => rerender({ instance: 'HYPATIA' }));

    // The beacon must carry the session's OWN instance (SALEM) — else SALEM's close
    // is sent to HYPATIA's backend and the SALEM session is stranded.
    expect(mockBeacon).toHaveBeenCalledWith('vs-1', 'SALEM');
    expect(mockBeacon).not.toHaveBeenCalledWith('vs-1', 'HYPATIA');
  });

  it('no mic is left live after a switch (the old session mic is stopped)', async () => {
    const tracks: FakeTrack[] = [];
    h.getUserMedia.mockImplementation(() => {
      const t = makeTrack();
      tracks.push(t);
      return Promise.resolve(new FakeMediaStream([t]));
    });
    const { result, rerender } = renderVoice('SALEM');
    await goLive(result);
    expect(tracks.length).toBe(1);

    act(() => rerender({ instance: 'HYPATIA' }));
    expect(tracks[0].stop).toHaveBeenCalled(); // old mic released
    expect(result.current.state).toBe('idle');
  });

  it('the unmount beacon also routes through the session instance', async () => {
    const { result, unmount } = renderVoice('HYPATIA');
    await goLive(result);
    unmount();
    expect(mockBeacon).toHaveBeenCalledWith('vs-1', 'HYPATIA');
  });

  it('a re-render with the SAME instance does NOT tear a live call down', async () => {
    const { result, rerender } = renderVoice('HYPATIA');
    await goLive(result);
    act(() => rerender({ instance: 'HYPATIA' }));
    expect(result.current.state).toBe('live'); // stable — no spurious teardown
    expect(mockBeacon).not.toHaveBeenCalled();
  });
});
