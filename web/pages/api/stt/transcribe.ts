import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import {
  MAX_AUDIO_BYTES,
  STT_IDEMPOTENCY_HEADER,
  isSttIdempotencyKey,
  normaliseAudioMime,
} from '../../../lib/algernon/schemas';
import { callTransportBinary } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// Binary BFF route for voice STT. bodyParser is DISABLED so Next does not
// JSON-parse the body or apply its default size cap — we stream the raw audio
// ourselves with our own 25 MiB guard. POST-only, session-gated (fail-closed
// 401), mime-allowlisted (415), size-capped (413), empty-rejected (400). The
// audio is relayed via callTransportBinary which injects the peer token
// server-side; this route holds NO secret. The transcript JSON (incl. the
// low_confidence/empty/degraded signals) relays back verbatim.
//
// Idempotency (lost-message #2): the client's content-hash idempotency header is
// ALLOWLISTED (only this one header is relayed, never arbitrary client headers) and
// forwarded to the backend, which dedups a repeat key → the cached transcript. It
// is relayed ONLY when it's a well-formed 64-char hex digest (a malformed/oversized
// value is dropped — the backend simply doesn't dedup, which is safe).
export const config = { api: { bodyParser: false } };

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  // Validate + normalise the mime BEFORE reading the body (cheap reject).
  const mime = normaliseAudioMime(req.headers['content-type']);
  if (!mime) {
    return res.status(415).json({ error: 'unsupported_media_type' });
  }

  // Stream the request into a Buffer with a hard cap — never request.read()/the
  // bodyParser, which would apply Next's own (smaller) limit.
  const chunks: Buffer[] = [];
  let total = 0;
  let tooLarge = false;
  try {
    for await (const chunk of req) {
      const buf: Buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
      total += buf.length;
      if (total > MAX_AUDIO_BYTES) {
        tooLarge = true;
        break;
      }
      chunks.push(buf);
    }
  } catch (e) {
    // The client's upload stream failed/aborted mid-read — closest contract code.
    console.error(`[bff:stt/transcribe] request stream read failed: ${(e as Error).message}`);
    return res.status(400).json({ error: 'no_audio' });
  }
  if (tooLarge) {
    return res.status(413).json({ error: 'audio_too_large' });
  }
  const audio = Buffer.concat(chunks);
  if (audio.length === 0) {
    return res.status(400).json({ error: 'no_audio' });
  }

  // Allowlist ONLY the idempotency header, and only when well-formed (a hex digest).
  const rawKey = req.headers[STT_IDEMPOTENCY_HEADER.toLowerCase()];
  const idempotencyKey = isSttIdempotencyKey(rawKey) ? rawKey : undefined;

  try {
    const { status, body } = await callTransportBinary('POST', '/stt/transcribe', {
      body: audio,
      contentType: mime,
      sessionToken,
      idempotencyKey,
    });
    return res.status(status).json(body ?? {});
  } catch (e) {
    return sendTransportError(res, 'stt/transcribe', e);
  }
}
