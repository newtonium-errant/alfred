import type { NextApiRequest, NextApiResponse } from 'next';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { listBatchTargets } from '../../../lib/algernon/transport';

// GET /api/batch/targets → the instances the owner can send a scan batch to
// ({name, label, home}) so the picker is data-driven (#90). Session-gated and
// OWNER-ONLY, matching the submit door: a non-owner cannot submit, so listing
// the deployment's instance topology to them would leak more than it serves.
//
// METADATA ONLY — no URL or token ever leaves the server.
//
// An EMPTY list is a real answer, not an error: it means nothing on this deploy
// is wired for bulk upload. The page renders an explicit empty state for it
// rather than an inert picker, so "not configured" is distinguishable from
// "configured and broken".
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
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

  return res.status(200).json({ targets: listBatchTargets() });
}
