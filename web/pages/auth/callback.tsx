import type { GetServerSideProps } from 'next';
import Head from 'next/head';
import { setSessionCookies } from '../../lib/algernon/identity';
import { safeNextPath } from '../../lib/algernon/safeNextPath';
import { authTokenSchema } from '../../lib/algernon/schemas';
import { TransportConfigError, callTransport } from '../../lib/algernon/transport';
import type { AuthVerifyResponse } from '../../lib/algernon/types';

// The magic-link target: {base_url}/auth/callback?token=<magic_token>. Verifies
// the token server-side (POST /auth/verify), sets the httpOnly session cookies,
// and redirects — so the cookie is set in the same response, no client JS needed
// and no token ever touches the browser's JS. The component below only renders
// on the (rare) non-redirect path.

function loginRedirect(error: string) {
  return { redirect: { destination: `/login?error=${error}`, permanent: false } } as const;
}

export const getServerSideProps: GetServerSideProps = async ({ query, res }) => {
  const rawToken = Array.isArray(query.token) ? query.token[0] : query.token;
  const parsed = authTokenSchema.safeParse(rawToken);
  if (!parsed.success) {
    return loginRedirect('missing_token');
  }

  const next = safeNextPath(Array.isArray(query.next) ? query.next[0] : query.next);

  try {
    const { status, body } = await callTransport('POST', '/auth/verify', {
      body: { token: parsed.data },
    });
    const v = (body ?? {}) as Partial<AuthVerifyResponse>;
    if (status === 200 && typeof v.session_token === 'string' && v.session_token) {
      setSessionCookies(res, {
        session_token: v.session_token,
        name: typeof v.name === 'string' ? v.name : '',
        role: typeof v.role === 'string' ? v.role : 'owner',
        exp: typeof v.exp === 'number' ? v.exp : undefined,
      });
      return { redirect: { destination: next, permanent: false } };
    }
    return loginRedirect('invalid_or_expired');
  } catch (e) {
    if (e instanceof TransportConfigError) return loginRedirect('not_configured');
    return loginRedirect('verify_failed');
  }
};

export default function AuthCallback() {
  return (
    <>
      <Head>
        <title>Signing you in…</title>
      </Head>
      <div className="flex min-h-screen items-center justify-center bg-honeydew-50 px-5">
        <p className="text-honeydew-700">Signing you in…</p>
      </div>
    </>
  );
}
