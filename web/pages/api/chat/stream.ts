import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { chatTurnBodySchema } from '../../../lib/algernon/schemas';
import { callChatStream, callTransportStream } from '../../../lib/algernon/transport';
import { gateCrossInstance, isHomeInstance } from '../../../lib/algernon/chatRouting';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/chat/stream → the SSE relay. ALL validation (auth/body/target) runs
// JSON-first and returns 401/400/403 BEFORE any stream byte; then the upstream
// transport SSE is passed THROUGH with no buffering (CONTRACT §1/§4). bodyParser
// is DISABLED (we read+parse the small JSON body ourselves, matching the STT
// route's raw-stream precedent) so Next does not buffer. The relay aborts on
// client disconnect — the backend detaches and finishes run_turn server-side
// (S4), so the FE reconciles via /chat/history if the stream dropped (S5). NO
// res.json() once streaming has started.
export const config = { api: { bodyParser: false, externalResolver: true } };

// Generous cap for a chat turn body (message ≤ 8000 chars + session_key +
// idempotency_key). Far under any DoS concern; rejects a runaway upload.
const MAX_BODY_BYTES = 128 * 1024;

async function readJsonBody(req: NextApiRequest): Promise<unknown> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buf: Buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
    total += buf.length;
    if (total > MAX_BODY_BYTES) throw new Error('body_too_large');
    chunks.push(buf);
  }
  const raw = Buffer.concat(chunks).toString('utf8');
  if (!raw.trim()) return {};
  return JSON.parse(raw);
}

function isEventStream(res: Response): boolean {
  return res.ok && (res.headers.get('content-type') || '').includes('text/event-stream');
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

  let rawBody: unknown;
  try {
    rawBody = await readJsonBody(req);
  } catch {
    return res.status(400).json({ error: 'invalid_request' });
  }

  const parsed = chatTurnBodySchema.safeParse(rawBody);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const payload = {
    session_key: parsed.data.session_key,
    message: parsed.data.message,
    kind: parsed.data.kind === 'voice' ? 'voice' : 'text',
    ...(parsed.data.idempotency_key ? { idempotency_key: parsed.data.idempotency_key } : {}),
  };

  const home = isHomeInstance(parsed.data.instance);
  let targetName = '';
  let userName = '';
  if (!home) {
    const gate = gateCrossInstance(req, parsed.data.instance as string);
    if (!gate.ok) {
      return res.status(gate.status).json(gate.body);
    }
    targetName = gate.targetName;
    userName = gate.userName;
  }

  // Drop the BFF↔transport relay when the browser disconnects. The backend keeps
  // run_turn running (detach, S4); only our write loop stops.
  const controller = new AbortController();
  req.on('close', () => {
    if (!res.writableEnded) controller.abort();
  });

  // Connect upstream BEFORE flushing any header, so an upstream JSON error
  // (401/400/404 returned before resp.prepare) relays as a clean JSON status,
  // not a 200 SSE that the browser would mistake for success.
  let upstream: Response;
  try {
    upstream = home
      ? await callTransportStream('POST', '/chat/stream', {
          body: payload,
          sessionToken,
          signal: controller.signal,
        })
      : await callChatStream(targetName, 'POST', '/chat/stream', {
          body: payload,
          userName,
          signal: controller.signal,
        });
  } catch (e) {
    return sendTransportError(res, 'chat/stream', e);
  }

  if (!isEventStream(upstream)) {
    const body = await upstream.json().catch(() => null);
    return res.status(upstream.status).json(body ?? { error: 'stream_failed' });
  }

  // Headers lock after this point — the response is now an SSE stream.
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders?.();
  res.socket?.setTimeout(0);

  try {
    // undici's Response.body is a web ReadableStream (async-iterable in Node 20).
    const body = upstream.body as unknown as AsyncIterable<Uint8Array> | null;
    if (body) {
      for await (const chunk of body) {
        res.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      }
    }
  } catch (e) {
    // The relay dropped. A client abort is expected (browser disconnected) — say
    // nothing. Otherwise emit a terminal SSE error frame so the FE distinguishes
    // a transport failure from a clean close (it will still reconcile via S5).
    if (!controller.signal.aborted && !res.writableEnded) {
      console.error(`[bff:chat/stream] relay error: ${(e as Error).message}`);
      res.write(`event: error\ndata: ${JSON.stringify({ error: 'transport_unreachable' })}\n\n`);
    }
  }
  if (!res.writableEnded) res.end();
}
