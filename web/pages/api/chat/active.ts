import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { callTransport } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// GET /api/chat/active → the caller's live session key on the home instance, or
// null when there is none (#94c). Session-gated like /api/chat/history.
//
// READ-ONLY by design. The client used to answer "do I have a session?" by
// calling /chat/open, which is close-prior-then-fresh — so asking the question
// destroyed the answer, and two devices each holding a stale key would kill
// each other's live session in a loop (three opens in 22s in the 2026-08-11
// log). This door cannot close anything.
//
// A null session_key is a 200, not a 404: absence is a normal answer here.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  try {
    const { status, body } = await callTransport('GET', '/chat/active', {
      sessionToken,
    });
    return res.status(status).json(body ?? { session_key: null, turns: 0 });
  } catch (e) {
    return sendTransportError(res, 'chat/active', e);
  }
}
