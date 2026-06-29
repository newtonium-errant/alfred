import type { NextApiRequest, NextApiResponse } from 'next';
import { resolveSessionToken } from '../../../lib/algernon/identity';
import { listCrossInstanceChatTargets } from '../../../lib/algernon/transport';
import { HOME_INSTANCE_NAME } from '../../../lib/algernon/instance';
import type { ChatTarget } from '../../../lib/algernon/types';

// GET /api/chat/targets → the chat instances the owner can talk to ({name, label,
// home}) so the FE switcher is data-driven. Session-gated (fail-closed 401 when
// signed out). Returns METADATA ONLY — no target URL or token ever leaves the
// server. The HOME instance is ALWAYS present (the default, routed via the
// existing session path); cross-instance relay targets come from
// ALFRED_WEB_CHAT_* env (each appears only when fully configured). A single-home
// list is valid — the FE hides the (no-op) picker when only home is configured.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const home: ChatTarget = { name: HOME_INSTANCE_NAME, label: HOME_INSTANCE_NAME, home: true };
  const cross: ChatTarget[] = listCrossInstanceChatTargets()
    // The home instance never appears as a relay target (it rides the session
    // path); drop any env target that collides with the home name.
    .filter((t) => t.name.toUpperCase() !== HOME_INSTANCE_NAME.toUpperCase())
    .map((t) => ({ name: t.name, label: t.label, home: false }));

  return res.status(200).json({ targets: [home, ...cross] });
}
