import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { appendFile, mkdir } from 'fs/promises';
import { dirname } from 'path';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { composerLogBodySchema } from '../../../lib/algernon/schemas';

// POST /api/feed/composer-log — CAPTURE-ONLY home-composer telemetry (B3-3). The
// composer picks brief/checkin/feed by the local hour; this records each
// composition + any within-10s navigation-away so the RULE can be tuned later
// (the learning is Phase D). It is NOT a transport route: it never relays, never
// calls the backend, and never carries evidence content — one JSONL line, done.
//
// Gates, in order (session + owner EXACTLY like ingest/submit + feed/act):
//   1. method (405)
//   2. session present (401 invalid_session) — fail-closed
//   3. OWNER-ONLY (403 forbidden) — the display-identity role guard
//   4. body shape (zod, 400 invalid_request) — z.object STRIPS unknown keys, so
//      a client can't smuggle extra fields into the log line
//
// APPEND SAFETY:
//   * The append path is SERVER-FIXED (env or default) — it is NEVER derived
//     from client input. The client's `path` field is stored as a DATA value in
//     the line, not used as a filesystem path.
//   * The line is JSON.stringify'd, so a `path` bearing a newline is escaped
//     (\n) and CANNOT forge a second JSONL record.
//   * A write failure NEVER fails the page: it is swallowed + console.warn'd and
//     the route still 200s (the composer's fetch is fire-and-forget anyway).

// Read the target at call time (not module load) so a per-request/test env is
// honoured. Server-fixed: env override or the default spool — never client input.
function composerLogPath(): string {
  return process.env.ALFRED_WEB_COMPOSER_LOG || './data/composer_log.jsonl';
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

  const parsed = composerLogBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { rule, event, dwell_ms, path } = parsed.data;
  const line =
    JSON.stringify({
      at: new Date().toISOString(),
      user: identity.name,
      rule,
      event,
      ...(dwell_ms !== undefined ? { dwell_ms } : {}),
      ...(path !== undefined ? { path } : {}),
    }) + '\n';

  try {
    const target = composerLogPath();
    await mkdir(dirname(target), { recursive: true }); // create ./data on a fresh deploy
    await appendFile(target, line, 'utf8'); // create-if-absent
  } catch (e) {
    // Telemetry must never break the page — swallow, warn, still 200.
    console.warn(`[bff:feed/composer-log] append_failed err=${(e as Error)?.message ?? 'unknown'}`);
    return res.status(200).json({ ok: true, logged: false });
  }

  return res.status(200).json({ ok: true, logged: true });
}
