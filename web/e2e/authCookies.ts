import type { BrowserContext } from '@playwright/test';

// Shared opt-in-smoke auth. The PAGE has its own gate (useSession → /api/auth/me →
// readDisplayIdentity), SEPARATE from the BFF's session check — a server-side dev
// token authenticates /api/voice/* but does NOT satisfy the page gate, so the
// browser redirects to /login and voice-start never renders. Plant the two
// production httpOnly cookies before goto('/'):
//   - algernon_session  = the session token (→ resolveSessionToken; also lets the
//                          BFF relay X-Alfred-Session without the dev-token env)
//   - algernon_identity = JSON {name, role} (→ readDisplayIdentity → page gate)
// Both values are encodeURIComponent-encoded to match the BFF's serializeCookie
// (lib/algernon/identity.ts) so Next's req.cookies decode round-trips — a RAW JSON
// identity value risks the browser rejecting the ',' / '"' cookie octets.
export async function plantSessionCookies(
  context: BrowserContext,
  url: string,
  token: string,
): Promise<void> {
  await context.addCookies([
    {
      name: 'algernon_session',
      value: encodeURIComponent(token),
      url,
      httpOnly: true,
      sameSite: 'Lax',
    },
    {
      name: 'algernon_identity',
      value: encodeURIComponent(JSON.stringify({ name: 'andrew', role: 'owner' })),
      url,
      httpOnly: true,
      sameSite: 'Lax',
    },
  ]);
}
