import type { NextApiRequest, NextApiResponse } from 'next';
import { readDisplayIdentity } from '../../../lib/algernon/identity';

// GET /api/auth/me → the signed-in user's DISPLAY identity ({name, role}) from
// the identity cookie, or 401 when signed out. This drives the chat page's
// auth gate + the header's name/sign-out. NOT an authorization decision — the
// session token (verified by the transport) is the sole authority for /chat/*.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }
  const identity = readDisplayIdentity(req);
  if (!identity) {
    return res.status(401).json({ error: 'not_authenticated' });
  }
  return res.status(200).json(identity);
}
