import { vi } from 'vitest';

// Shared scripted WebRTC fakes for the voice hook tests (V0 + V1). Extracted from
// useVoice.test.ts so useVoiceDictation.test.ts can drive the same peer connection.
// V1 additions: FakeDataChannel + createDataChannel on the pc + an `ops` ordering
// log so a test can pin that the datachannel is created BEFORE the offer (the fake
// won't otherwise fail on wrong order).

export interface FakeTrack {
  kind: string;
  enabled: boolean;
  stop: ReturnType<typeof vi.fn>;
}

export function makeTrack(): FakeTrack {
  return { kind: 'audio', enabled: true, stop: vi.fn() };
}

export class FakeMediaStream {
  constructor(private tracks: FakeTrack[] = []) {}
  getTracks() {
    return this.tracks;
  }
  getAudioTracks() {
    return this.tracks;
  }
}

export class FakeDataChannel {
  label: string;
  readyState: 'connecting' | 'open' | 'closing' | 'closed' = 'connecting';
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(label: string) {
    this.label = label;
  }

  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 'closed';
  }

  // --- test drivers ---
  emitOpen() {
    this.readyState = 'open';
    this.onopen?.();
  }
  /** Emit a server frame (object → JSON, string → verbatim). */
  emitMessage(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.onmessage?.({ data });
  }
  /** Emit a raw payload untouched (for non-string / malformed-JSON drops). */
  emitRaw(data: unknown) {
    this.onmessage?.({ data });
  }
  emitClose() {
    this.readyState = 'closed';
    this.onclose?.();
  }
  emitError() {
    this.onerror?.();
  }
}

type Listener = () => void;

export class FakeRTCPeerConnection {
  static instances: FakeRTCPeerConnection[] = [];
  static autoGather = true;

  // When set, the NEXT constructor throws (one-shot) — simulates a transient
  // RTCPeerConnection construction failure after a network transition.
  static failNextConstruct = false;

  static reset() {
    FakeRTCPeerConnection.instances = [];
    FakeRTCPeerConnection.autoGather = true;
    FakeRTCPeerConnection.failNextConstruct = false;
  }

  config: RTCConfiguration;
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: unknown = null;
  iceGatheringState = 'new';
  connectionState = 'new';
  ontrack: ((ev: { streams: FakeMediaStream[]; track: unknown }) => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  closed = false;
  tracks: Array<{ track: FakeTrack; stream: FakeMediaStream }> = [];
  channels: FakeDataChannel[] = [];
  // Ordered call log: e.g. ['createDataChannel:voice', 'createOffer'].
  ops: string[] = [];
  private listeners: Record<string, Listener[]> = {};

  constructor(config: RTCConfiguration) {
    if (FakeRTCPeerConnection.failNextConstruct) {
      FakeRTCPeerConnection.failNextConstruct = false;
      throw new DOMException('construction failed', 'InvalidStateError');
    }
    this.config = config;
    FakeRTCPeerConnection.instances.push(this);
  }

  addTrack(track: FakeTrack, stream: FakeMediaStream) {
    this.tracks.push({ track, stream });
  }
  createDataChannel(label: string) {
    this.ops.push(`createDataChannel:${label}`);
    const dc = new FakeDataChannel(label);
    this.channels.push(dc);
    return dc;
  }
  addEventListener(type: string, cb: Listener) {
    (this.listeners[type] ||= []).push(cb);
  }
  removeEventListener(type: string, cb: Listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== cb);
  }
  async createOffer() {
    this.ops.push('createOffer');
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

  // --- test drivers ---
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
  lastChannel(): FakeDataChannel {
    const c = this.channels.at(-1);
    if (!c) throw new Error('no datachannel was created');
    return c;
  }
}

// Install the peer-connection + media-stream fakes onto globals. Returns the mic
// track + a getUserMedia/fetch mock + an audio-element stub the hook writes to.
export interface VoiceGlobals {
  micTrack: FakeTrack;
  getUserMedia: ReturnType<typeof vi.fn>;
  fetchMock: ReturnType<typeof vi.fn>;
  audioEl: { srcObject: unknown; muted: boolean; play: ReturnType<typeof vi.fn> };
  audioRef: { current: { srcObject: unknown; muted: boolean; play: ReturnType<typeof vi.fn> } };
  setPlay: (resolves: boolean) => void;
}

export function installVoiceGlobals(): VoiceGlobals {
  FakeRTCPeerConnection.reset();
  (global as unknown as { RTCPeerConnection: unknown }).RTCPeerConnection = FakeRTCPeerConnection;
  (global as unknown as { MediaStream: unknown }).MediaStream = FakeMediaStream;

  const micTrack = makeTrack();
  const getUserMedia = vi.fn().mockResolvedValue(new FakeMediaStream([micTrack]));
  Object.defineProperty(global.navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  });
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  (global as unknown as { fetch: unknown }).fetch = fetchMock;

  // `muted` is the ONLY new element property V2 touches (speaker-mute); no pause,
  // no sample content — the fakes stay state/property-only.
  const audioEl = { srcObject: null as unknown, muted: false, play: vi.fn().mockResolvedValue(undefined) };
  const audioRef = { current: audioEl };
  const setPlay = (resolves: boolean) => {
    audioEl.play = resolves
      ? vi.fn().mockResolvedValue(undefined)
      : vi.fn().mockRejectedValue(new DOMException('blocked', 'NotAllowedError'));
  };

  return { micTrack, getUserMedia, fetchMock, audioEl, audioRef, setPlay };
}

export function lastPC(): FakeRTCPeerConnection {
  const pc = FakeRTCPeerConnection.instances.at(-1);
  if (!pc) throw new Error('no RTCPeerConnection was constructed');
  return pc;
}
