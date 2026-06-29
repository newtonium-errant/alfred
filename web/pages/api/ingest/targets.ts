import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { listIngestTargets } from '../../../lib/algernon/transport';

// GET /api/ingest/targets → the configured ingest targets ({name, label,
// recordTypes}) so the FE picker is data-driven. Session-gated (fail-closed 401
// when signed out). Returns METADATA ONLY — no target URL or token ever leaves
// the server (listIngestTargets returns no secrets). An empty list is a valid
// response (no targets configured) — the page renders an explicit empty state.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  return res.status(200).json({ targets: listIngestTargets() });
}
