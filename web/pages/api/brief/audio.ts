import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransportRaw } from '../../../lib/algernon/transport';
import { mapUpstreamWrongPeer, sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/brief/audio[?speed=0.7-1.2] → the day's briefing mp3 (c2's C3a
// GET /web/brief/audio). Forwards the transport response by content-type: audio/mpeg
// bytes on a cache hit / render, or a JSON ILB state (no_brief / tts_not_configured /
// any future state) the FE branches on. Session-authed (X-Alfred-Session, same as the
// brief route). Replay/scrub is cached backend-side → zero credits.

export const config = {
  // The mp3 body can exceed Next's default 4mb API response cap; allow the stream.
  api: { responseLimit: false },
};

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  // `speed` is optional; only a well-formed value inside the backend's 0.7-1.2 clamp is
  // forwarded (else omit → provider default). Never interpolate an attacker-shaped value.
  const rawSpeed = typeof req.query.speed === 'string' ? Number(req.query.speed) : Number.NaN;
  const speedQs = Number.isFinite(rawSpeed) && rawSpeed >= 0.7 && rawSpeed <= 1.2 ? `?speed=${rawSpeed}` : '';

  try {
    const upstream = await callTransportRaw('GET', `/web/brief/audio${speedQs}`, { sessionToken });
    const contentType = (upstream.headers.get('content-type') || '').toLowerCase();
    if (contentType.includes('audio/mpeg')) {
      const buf = Buffer.from(await upstream.arrayBuffer());
      res.setHeader('Content-Type', 'audio/mpeg');
      const cache = upstream.headers.get('x-brief-audio-cache');
      if (cache) res.setHeader('X-Brief-Audio-Cache', cache); // informational hit|miss
      return res.status(upstream.status).send(buf);
    }
    // A JSON ILB state (no_brief / tts_not_configured / a future state) or an error body
    // — forward status + JSON verbatim; the FE's playerAudioState maps it (unknown → text-along).
    let body: unknown = null;
    try {
      body = await upstream.json();
    } catch {
      body = null;
    }
    // A post-auth wrong_peer 401 (BFF peer misconfig) → 502, never a fake logout; a real
    // invalid_session 401 relays for the re-login path. (The raw proxy must map this
    // itself — callTransportRaw returns the status verbatim; see its docstring.)
    if (mapUpstreamWrongPeer(res, 'brief/audio', upstream.status, body)) return;
    return res.status(upstream.status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'brief/audio', e);
  }
}
