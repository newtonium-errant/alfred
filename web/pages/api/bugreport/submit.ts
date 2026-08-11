import type { NextApiRequest, NextApiResponse } from 'next';
import type { ZodIssue } from 'zod';
import { readDisplayIdentity, resolveSessionToken } from '../../../lib/algernon/identity';
import { bugReportBodySchema } from '../../../lib/algernon/schemas';
import { callTransportBugReport, isBugReportConfigured } from '../../../lib/algernon/transport';
import { sendTransportError } from '../../../lib/algernon/bffError';

// POST /api/bugreport/submit → relays one bug report to the HOME instance's
// transport POST /vault/bugreport, using the server-side `web_bugreport` peer
// token. Gates, in order:
//   1. method (405)
//   2. feature configured (503 not_configured) — checked BEFORE auth so a
//      deploy that hasn't added the token yet answers "not switched on" rather
//      than "you're not allowed", which are different facts and send an
//      operator looking in different places.
//   3. session present (401) — fail-closed, not signed in
//   4. body shape (zod, 400)
// `reported_by` is the verified display name (provenance metadata only, never
// authz — possession of the peer token is the write authority). Backend errors
// (413 screenshot_too_large, 400 empty_description, …) relay through VERBATIM so
// the modal can map each code to its own sentence; transport/config failures map
// via sendTransportError (no topology leak).
//
// DELIBERATELY NOT OWNER-ONLY, unlike ingest/submit. Ingest writes an operator's
// chosen artifact into the vault as a record; this files a complaint about the
// app. Anyone who can sign in can hit a bug — gating the report button on a role
// would mean the users most likely to encounter a broken screen are the ones who
// cannot tell anybody about it. The box's caps and the peer pin are what bound
// the blast radius, and neither depends on the reporter's role.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }

  if (!isBugReportConfigured()) {
    // Deploy-inert: the feature ships dark and lights up when the operator adds
    // ALFRED_WEB_BUGREPORT_TOKEN. Named explicitly so "the button did nothing"
    // is never the diagnosis.
    return res.status(503).json({ error: 'bugreport_not_configured' });
  }

  const sessionToken = resolveSessionToken(req);
  if (!sessionToken) {
    return res.status(401).json({ error: 'invalid_session' });
  }

  const identity = readDisplayIdentity(req);

  const parsed = bugReportBodySchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      error: 'invalid_request',
      detail: parsed.error.issues.map((i: ZodIssue) => i.message).join('; '),
    });
  }

  const { description, context, screenshot_b64, screenshot_media_type } = parsed.data;

  try {
    const { status, body: respBody } = await callTransportBugReport('/vault/bugreport', {
      user: identity?.name,
      body: {
        description,
        context,
        // Spread rather than always-send: an explicit `screenshot_b64:
        // undefined` still serialises differently from an absent key, and a
        // report with no screenshot should put the same bytes on the wire it
        // always did.
        ...(screenshot_b64
          ? { screenshot_b64, screenshot_media_type: screenshot_media_type ?? 'image/png' }
          : {}),
        reported_by: identity?.name ?? '',
      },
    });
    return res.status(status).json(respBody ?? {});
  } catch (e) {
    return sendTransportError(res, 'bugreport/submit', e);
  }
}
