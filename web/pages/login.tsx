import { FormEvent, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Card } from '../components/ui/card';
import { Input } from '../components/ui/input';
import { Button } from '../components/ui/button';
import { Label } from '../components/ui/label';
import { authApi } from '../lib/algernon/authClient';
import { ApiError } from '../lib/algernon/http';
import { safeNextPath } from '../lib/algernon/safeNextPath';

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

// Maps an OTP request/verify ApiError to a friendly line.
function otpErrorMessage(err: unknown, phase: 'request' | 'verify'): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case 'otp_disabled':
        return 'Code sign-in isn’t enabled on this instance yet. Use a sign-in link instead.';
      case 'invalid_or_expired':
        return 'That code is incorrect or has expired. Try again or request a new one.';
      case 'email_not_configured':
        return 'Email sign-in isn’t configured on this instance yet.';
      case 'email_required':
        return 'Please enter your email address.';
      case 'rate_limited':
        return 'Too many code requests — wait a few minutes and try again.';
    }
  }
  return phase === 'request'
    ? 'Couldn’t send your code just now. Please try again.'
    : 'Couldn’t verify that code just now. Please try again.';
}

// Magic-link sign-in: enter email → backend emails a link → click it → the
// /auth/callback page verifies + sets the session cookie. No password, no code to
// type. Borrows honeydew's centered card layout (Supabase data edge replaced by
// the BFF /api/auth/login call).
//
// OTP sign-in (parity #23 — "Sign in with a code"): the iOS PWA path. A magic
// link tapped from Mail opens in Safari — a DIFFERENT cookie jar from the
// installed PWA — so the PWA stays signed out. The code flow keeps everything
// in THIS open page: request a 6-digit code, type it in, the BFF verifies and
// sets the httpOnly session cookie on this same-origin response (the PWA's own
// jar), then we client-redirect to `next`. The affordance is always shown and
// degrades gracefully when the instance hasn't enabled OTP (otp_disabled).
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

  // OTP ("sign in with a code") state. `method` defaults to the magic link so
  // the existing desktop flow is byte-identical; `otpStage` advances from the
  // email form to the code entry once a code has been requested.
  const [method, setMethod] = useState<'link' | 'code'>('link');
  const [otpStage, setOtpStage] = useState<'request' | 'code'>('request');
  const [code, setCode] = useState('');
  const [otpBusy, setOtpBusy] = useState(false);
  const [otpError, setOtpError] = useState<string | null>(null);

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

  async function handleOtpRequest(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setOtpError(null);
    setOtpBusy(true);
    try {
      await authApi.otpRequest(email.trim());
      setCode('');
      setOtpStage('code');
    } catch (err) {
      setOtpError(otpErrorMessage(err, 'request'));
    } finally {
      setOtpBusy(false);
    }
  }

  async function handleOtpVerify(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setOtpError(null);
    setOtpBusy(true);
    try {
      await authApi.otpVerify(email.trim(), code.trim());
      // Cookie was set server-side on the response we just received — the
      // session lives in THIS context's jar. Client-guard the deep-link
      // (mirrors auth/callback's safeNextPath re-guard) and go.
      await router.replace(safeNextPath(nextParam));
    } catch (err) {
      setOtpError(otpErrorMessage(err, 'verify'));
      setOtpBusy(false);
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

          {method === 'link' ? (
            <>
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

              {!sent && (
                <button
                  type="button"
                  data-testid="otp-toggle"
                  onClick={() => {
                    setMethod('code');
                    setError(null);
                  }}
                  className="mt-5 text-sm text-honeydew-600 underline hover:text-honeydew-700"
                >
                  Sign in with a code instead (best on iPhone)
                </button>
              )}
            </>
          ) : (
            <>
              {otpStage === 'request' ? (
                <>
                  <p className="mt-2 text-honeydew-600/90">
                    We’ll email you a 6-digit code — type it in right here to sign in.
                    Best for the installed app on iPhone.
                  </p>
                  <form onSubmit={handleOtpRequest} className="mt-5 flex flex-col gap-3">
                    <Label htmlFor="otp-email-input">Email</Label>
                    <Input
                      id="otp-email-input"
                      data-testid="otp-email-input"
                      type="email"
                      required
                      autoComplete="email"
                      placeholder="you@example.com"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                    />
                    <Button
                      type="submit"
                      data-testid="otp-request-submit"
                      disabled={otpBusy}
                      className="w-full"
                    >
                      {otpBusy ? 'Sending…' : 'Email me a code'}
                    </Button>
                  </form>
                </>
              ) : (
                <div data-testid="otp-code-stage">
                  <p className="mt-3 text-honeydew-600">
                    Check your email — if{' '}
                    <strong className="text-honeydew-700">{email.trim()}</strong> is on the
                    list, a 6-digit code is on its way. Enter it below.
                  </p>
                  <form onSubmit={handleOtpVerify} className="mt-5 flex flex-col gap-3">
                    <Label htmlFor="otp-code-input">6-digit code</Label>
                    <Input
                      id="otp-code-input"
                      data-testid="otp-code-input"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      pattern="[0-9]{6}"
                      maxLength={6}
                      required
                      placeholder="123456"
                      value={code}
                      onChange={(e) => setCode(e.target.value.replace(/[^0-9]/g, ''))}
                    />
                    <Button
                      type="submit"
                      data-testid="otp-verify-submit"
                      disabled={otpBusy || code.trim().length !== 6}
                      className="w-full"
                    >
                      {otpBusy ? 'Signing in…' : 'Sign in'}
                    </Button>
                  </form>
                  <button
                    type="button"
                    data-testid="otp-resend"
                    onClick={() => {
                      setOtpStage('request');
                      setOtpError(null);
                    }}
                    className="mt-4 text-sm text-honeydew-600 hover:text-honeydew-700"
                  >
                    ← Didn’t get it? Send a new code
                  </button>
                </div>
              )}

              {otpError && (
                <p data-testid="otp-error" role="alert" className="mt-4 text-sm text-danger">
                  {otpError}
                </p>
              )}

              <button
                type="button"
                data-testid="link-toggle"
                onClick={() => {
                  setMethod('link');
                  setOtpError(null);
                  setOtpStage('request');
                }}
                className="mt-5 block text-sm text-honeydew-600 underline hover:text-honeydew-700"
              >
                Use an emailed sign-in link instead
              </button>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
