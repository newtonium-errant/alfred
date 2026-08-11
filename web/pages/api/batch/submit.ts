import type { NextApiRequest, NextApiResponse } from 'next';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransportBatch, listBatchTargets } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';
import {
  BATCH_IDEMPOTENCY_HEADER,
  isBatchIdempotencyKey,
} from '../../../lib/algernon/batchSubmit';

// POST /api/batch/submit → relays a multipart bulk scan submission to the box's
// POST /vault/batch, using the server-side `web_batch` peer token.
//
// A BYTE PIPE. Next's body parser is OFF and the raw body is forwarded with its
// original Content-Type (boundary included). The BFF deliberately does NOT
// parse the multipart: a second multipart implementation here would have its
// own limits, and the box's are the authority — it is the only layer a client
// cannot skip. So this door does auth and nothing else.
//
// Gates, in order:
//   1. method (405)
//   2. session present (401) — fail-closed, not signed in
//   3. OWNER-ONLY (403) — role from the display identity cookie. The cookie is
//      provenance/display, never the WRITE authority; the peer token (BFF-only)
//      is. Defence in depth on top of the box's peer pin.
//   4. configured (500 via sendTransportError) — fail-closed, no half-config
//
// `X-Alfred-Batch-User` carries the verified display name as PROVENANCE only.
// Possession of the `web_batch` token is the authority to write, which is why
// the header is never consulted for permission on either side.

export const config = {
  api: {
    // The whole point: a 128 MiB multipart body must reach the relay unparsed.
    // Next's default parser would both reject it and destroy the boundary.
    bodyParser: false,
    // Above the box's 128 MiB total-batch cap so an oversize submission is
    // refused BY THE BOX with its own error code and a sentence naming which
    // cap it hit — rather than by Next, with a generic body-size error the
    // operator cannot act on. Same ordering doctrine as the transport app's
    // client_max_size sitting above the ingest route's own cap.
    responseLimit: false,
  },
};

const MAX_RELAY_BYTES = 160 * 1024 * 1024;

/** Collect the raw request body. Bounded so a hung upload cannot exhaust RAM. */
async function readRawBody(req: NextApiRequest): Promise<Buffer | null> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buf.length;
    if (total > MAX_RELAY_BYTES) return null;
    chunks.push(buf);
  }
  return Buffer.concat(chunks);
}

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const identity = readDisplayIdentity(req);
  if (!identity || identity.role !== 'owner') {
    return res.status(403).json({ error: 'forbidden' });
  }

  // Which instance (#90). The selector rides the QUERY STRING, not the body:
  // this route's body is raw multipart bytes with the parser off, so reading a
  // form field would mean parsing the multipart here — the exact second
  // implementation the byte-pipe design exists to avoid.
  const rawTarget = req.query.target;
  const target = (Array.isArray(rawTarget) ? rawTarget[0] : rawTarget || '').trim();

  // Validate against the CONFIGURED list before touching any env, so a bogus
  // name cannot probe which instances exist. An empty selector means home.
  const targets = listBatchTargets();
  if (targets.length === 0) {
    // Fail-closed and NAMED: "not wired up here" is a different fact from "the
    // instance refused you", and the operator can act on only one of them.
    return res.status(503).json({ error: 'transport_misconfigured' });
  }
  const chosen = target
    ? targets.find((t) => t.name.toUpperCase() === target.toUpperCase())
    : targets.find((t) => t.home) || targets[0];
  if (!chosen) {
    // NAMES the instance. "Unknown target" alone would leave the operator
    // guessing whether they mistyped it or it was never configured, and the
    // answer is the same env pair either way.
    return res.status(400).json({ error: 'unknown_target', target });
  }

  const contentType = req.headers['content-type'] || '';
  if (!contentType.toLowerCase().startsWith('multipart/')) {
    // Checked here as well as on the box so a malformed client request does not
    // spend a 128 MiB relay to be told the same thing.
    return res.status(415).json({ error: 'not_multipart' });
  }

  const body = await readRawBody(req);
  if (body === null) {
    return res.status(413).json({ error: 'batch_too_large' });
  }

  // Allowlist ONLY the idempotency header (#100), and only when well-formed —
  // the same discipline /api/stt/transcribe applies to its own key. The BFF
  // never MINTS one: a key minted per relay would differ on every retry, which
  // is precisely the double-mint the key exists to prevent. A malformed value is
  // dropped rather than relayed, so the submission simply runs undeduped.
  const rawKey = req.headers[BATCH_IDEMPOTENCY_HEADER.toLowerCase()];
  const idempotencyKey = isBatchIdempotencyKey(rawKey) ? rawKey : undefined;

  try {
    const { status, body: respBody } = await callTransportBatch('/vault/batch', {
      body,
      contentType,
      user: identity.name,
      target: chosen.name,
      idempotencyKey,
    });
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'batch/submit', e);
  }
}
