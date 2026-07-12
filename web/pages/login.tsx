import { FormEvent, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Card } from '../components/ui/card';
import { Input } from '../components/ui/input';
import { Button } from '../components/ui/button';
import { Label } from '../components/ui/label';
import { authApi } from '../lib/algernon/authClient';
import { ApiError } from '../lib/algernon/http';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Maps a ?error=… from a failed magic-link callback into a friendly line.
function callbackErrorMessage(code: string | string[] | undefined): string | null {
  const c = Array.isArray(code) ? code[0] : code;
  switch (c) {
    case 'invalid_or_expired':
      return 'That sign-in link has expired or was already used. Request a fresh one below.';
    case 'missing_token':
      return 'That sign-in link was incomplete. Request a fresh one below.';
    case 'not_configured':
      return 'Email sign-in isn’t configured on this instance yet.';
    case 'verify_failed':
      return 'Couldn’t verify that link just now. Try requesting a new one.';
    default:
      return null;
  }
}

// Magic-link sign-in: enter email → backend emails a link → click it → the
// /auth/callback page verifies + sets the session cookie. No password, no code to
// type. Borrows honeydew's centered card layout (Supabase data edge replaced by
// the BFF /api/auth/login call).
export default function LoginPage() {
  const router = useRouter();
  // Where to land after sign-in (deep-link). Passed to the backend, which embeds
  // it in the magic link; auth/callback re-guards it via safeNextPath. A repeated
  // ?next= yields string[] — take the first.
  const nextParam = Array.isArray(router.query.next) ? router.query.next[0] : router.query.next;
  const [email, setEmail] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const callbackError = callbackErrorMessage(router.query.error);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSending(true);
    try {
      await authApi.login(email.trim(), nextParam);
      setSent(true);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'email_not_configured') {
        setError('Email sign-in isn’t configured on this instance yet.');
      } else if (err instanceof ApiError && err.code === 'email_required') {
        setError('Please enter your email address.');
      } else {
        setError('Couldn’t send your sign-in link just now. Please try again.');
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <Head>
        <title>Sign in · {INSTANCE_NAME}</title>
      </Head>
      <div className="flex min-h-screen flex-col items-center justify-center bg-honeydew-50 px-5 py-10">
        <div className="mb-6 flex items-center gap-2 text-2xl font-extrabold text-honeydew-700">
          <span aria-hidden="true">✦</span> {INSTANCE_NAME}
        </div>
        <Card className="w-full max-w-md p-7">
          <h1 className="text-xl font-extrabold text-honeydew-700">Sign in</h1>

          {sent ? (
            <div data-testid="login-sent">
              <p className="mt-3 text-honeydew-600">
                Check your email — if{' '}
                <strong className="text-honeydew-700">{email.trim()}</strong> is on the
                list, a sign-in link is on its way. Open it on this device to continue.
              </p>
              <button
                type="button"
                data-testid="login-back"
                onClick={() => {
                  setSent(false);
                  setError(null);
                }}
                className="mt-4 text-sm text-honeydew-600 hover:text-honeydew-700"
              >
                ← Use a different email
              </button>
            </div>
          ) : (
            <>
              <p className="mt-2 text-honeydew-600/90">
                Enter your email and we’ll send you a sign-in link — no password needed.
              </p>
              <form onSubmit={handleSubmit} className="mt-5 flex flex-col gap-3">
                <Label htmlFor="email-input">Email</Label>
                <Input
                  id="email-input"
                  data-testid="email-input"
                  type="email"
                  required
                  autoComplete="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
                <Button
                  type="submit"
                  data-testid="login-submit"
                  disabled={sending}
                  className="w-full"
                >
                  {sending ? 'Sending…' : 'Email me a sign-in link'}
                </Button>
              </form>
            </>
          )}

          {(error || (callbackError && !sent)) && (
            <p data-testid="login-error" role="alert" className="mt-4 text-sm text-danger">
              {error || callbackError}
            </p>
          )}
        </Card>
      </div>
    </>
  );
}
