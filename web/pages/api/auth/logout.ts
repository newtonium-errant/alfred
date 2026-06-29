import type { NextApiRequest, NextApiResponse } from 'next';
import { clearSessionCookies } from '../../../lib/algernon/identity';

// POST /api/auth/logout → clears the httpOnly session + identity cookies. Purely
// local (no transport call); the instance-signed token simply stops being sent.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }
  clearSessionCookies(res);
  return res.status(200).json({ status: 'ok' });
}
