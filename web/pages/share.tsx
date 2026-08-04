import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { EmptyState } from '../components/EmptyState';
import { IngestForm } from '../components/ingest/IngestForm';
import { authApi } from '../lib/algernon/authClient';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle } from '../lib/typography';
import {
  SHARE_RESTORE_PATH,
  clearStashedCapture,
  parseSharedCapture,
  readStashedCapture,
  stashSharedCapture,
  type SharedCapture,
} from '../lib/algernon/shareTarget';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';
const SIGN_IN_HREF = `/login?next=${encodeURIComponent(SHARE_RESTORE_PATH)}`;

// Web Share Target handler (G2). The system share sheet navigates the installed
// PWA here with the payload in the query string (mapping declared in
// manifest.webmanifest); this page normalises it and hands it to the SAME
// session-authed ingest form /ingest uses.
//
// IT DOES NOT AUTO-SUBMIT, deliberately. A share should land in a surface the
// operator can see and correct: the target instance is a real choice, the
// derived title is a guess, and a silent write would give a shared selection
// less review than a typed one. Auto-submit also has nowhere to put a failure —
// the share sheet is already gone by then.
//
// NOTHING IS EVER DROPPED. The capture is parked in sessionStorage the moment it
// arrives, BEFORE the auth branch, so a signed-out share survives the sign-in
// round trip. It cannot ride the redirect itself: `safeNextPath` rejects any
// codepoint <= 0x20, so a `next=` carrying real shared text (which almost always
// contains a space) would be downgraded to '/' and the words lost.
export default function SharePage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const [capture, setCapture] = useState<SharedCapture | null>(null);

  useEffect(() => {
    const shared = parseSharedCapture(router.query ?? {}, new Date());
    if (!shared.empty) {
      stashSharedCapture(shared);
      setCapture(shared);
      return;
    }
    // No payload on this navigation: either a restore after signing in, or a
    // bare visit. Never clobber a capture already in hand.
    setCapture((prev) => prev ?? readStashedCapture());
  }, [router.query]);

  const isOwner = !sessionLoading && user?.role === 'owner';
  const formReady = isOwner && capture !== null;

  // Once the capture is in the form it lives in form state; drop the parked copy
  // so a later unrelated visit to /share can't resurrect a stale share.
  useEffect(() => {
    if (formReady) clearStashedCapture();
  }, [formReady]);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* clearing is best-effort; redirect regardless */
    }
    router.replace('/login');
  }, [router]);

  const onUnauthenticated = useCallback(() => {
    router.replace(SIGN_IN_HREF);
  }, [router]);

  const head = (
    <Head>
      <title>Share · {INSTANCE_NAME}</title>
    </Head>
  );

  // Intentionally-left-blank: an explicit loading line, never a blank pane.
  if (sessionLoading) {
    return (
      <>
        {head}
        <Layout showNav={false}>
          <p data-testid="share-auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  // Signed out. Unlike /ingest we do NOT bounce straight to /login — this page is
  // holding the operator's words, so it says so, shows them, and offers the door.
  if (!user) {
    return (
      <>
        {head}
        <Layout showNav={false}>
          <div className="min-w-0">
            <h1 className={display}>Sign in to file this</h1>
            <p className={`mt-1 ${subtle}`}>
              {capture
                ? 'Your shared text is held here and will still be waiting after you sign in.'
                : 'Sign in to capture shares into your vault.'}
            </p>
          </div>
          {capture && (
            <p
              data-testid="share-held-preview"
              className="mt-4 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800"
            >
              {capture.body}
            </p>
          )}
          <div className="mt-6">
            <a
              data-testid="share-sign-in"
              href={SIGN_IN_HREF}
              className="inline-flex items-center rounded-xl bg-honeydew-600 px-4 py-2 text-sm font-semibold text-white hover:bg-honeydew-700"
            >
              Sign in
            </a>
          </div>
        </Layout>
      </>
    );
  }

  return (
    <>
      {head}
      <Layout onSignOut={() => void handleSignOut()}>
        <div className="min-w-0">
          <h1 className={display}>Share to {INSTANCE_NAME}</h1>
          <p className={`mt-1 ${subtle}`}>
            Review what was shared, pick where it lands, then file it.
          </p>
        </div>

        <div className="mt-6">
          {!isOwner ? (
            // Intentionally-left-blank: explicit owner-only state, not a dead form.
            <EmptyState
              icon="🔒"
              title="Owner only"
              message="Capturing shares is restricted to the instance owner."
              testId="share-owner-only"
            />
          ) : !capture ? (
            // Intentionally-left-blank: reaching /share directly is a real state,
            // distinct from a share that failed to arrive.
            <EmptyState
              icon="📤"
              title="Nothing shared yet"
              message="Share text from another app and pick Algernon to capture it here. You can also write a document directly on the Ingest page."
              testId="share-nothing"
            />
          ) : (
            <IngestForm
              user={user}
              originInstance={INSTANCE_NAME}
              onUnauthenticated={onUnauthenticated}
              initialTitle={capture.title}
              initialBody={capture.body}
              initialSource={capture.source}
            />
          )}
        </div>
      </Layout>
    </>
  );
}
