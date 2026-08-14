import '../styles/globals.css';
// The two Phase B surface skins, both imported AFTER globals so their
// surface-scoped overrides (the console's focus ring, the sensor log's own)
// win on order. Each is additive and opt-in: neither defines a rule that is
// not scoped under its surface's `data-surface` attribute, so importing both
// unconditionally restyles nothing that has not asked for it.
import '../styles/console.css';
import '../styles/sensorLog.css'; // feed surface skin + token seam; see that file's header
// The console-completion registers. Same contract as the two above: each is
// additive and opt-in, defines no rule that is not scoped under its own
// `data-surface` attribute or an owned class, and re-points at the shared
// `--console-*` layer rather than holding hexes of its own.
import '../styles/viewscreen.css';
import '../styles/crt.css';
import '../styles/comms.css';
import type { AppProps } from 'next/app';
import Head from 'next/head';
import { Nunito } from 'next/font/google';
import { useEffect } from 'react';
import { ContactRouteToast } from '../components/ContactRouteToast';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Friendly rounded sans, self-hosted by next/font (no external <link>). Exposed
// as a CSS variable so Tailwind's fontFamily.sans (var(--font-honeydew)) picks
// it up across the whole app, with a system fallback. Borrowed from honeydew —
// the warmth doctrine: rounded font, solid weights, never thin/light.
const nunito = Nunito({
  subsets: ['latin'],
  weight: ['400', '600', '700', '800'],
  variable: '--font-honeydew',
  display: 'swap',
  fallback: ['ui-rounded', 'system-ui', 'sans-serif'],
});

// PWA shell (M2): manifest + icons + meta below, service worker registered in the
// effect. The SW (public/sw.js) gives an installable app + offline shell but NEVER
// caches /api/* or /auth/* — auth/session/chat/SSE always hit the network.
export default function App({ Component, pageProps }: AppProps) {
  useEffect(() => {
    if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return;
    // Register after load so SW install/precache doesn't contend with first paint.
    const register = () => {
      navigator.serviceWorker.register('/sw.js').catch((err) => {
        // Non-fatal: the app works fully without the SW (no offline/install only).
        console.error('[pwa] service worker registration failed:', err);
      });
    };
    if (document.readyState === 'complete') {
      register();
      return;
    }
    window.addEventListener('load', register, { once: true });
    return () => window.removeEventListener('load', register);
  }, []);

  return (
    <div className={`${nunito.variable} font-sans`}>
      <Head>
        <title>{INSTANCE_NAME}</title>
        <meta
          name="description"
          content={`Chat with ${INSTANCE_NAME}, grounded in your vault.`}
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="theme-color" content="#7bbf4f" />

        {/* PWA: installable manifest + icons (single Flowers-for-Algernon source). */}
        <link rel="manifest" href="/manifest.webmanifest" />
        <link rel="icon" href="/favicon.ico" sizes="any" />
        <link rel="icon" type="image/svg+xml" href="/icon.svg" />
        <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png" />
        <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" />

        {/* iOS standalone install + status bar. */}
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="default" />
        <meta name="apple-mobile-web-app-title" content={INSTANCE_NAME} />
        <meta name="mobile-web-app-capable" content="yes" />
      </Head>
      <Component {...pageProps} />
      {/* C4 contact-surface router: the override affordance for a routed open.
          Mounted HERE rather than per-page because the decision is made on the
          landing and rendered on the destination — a client-side navigation
          unmounts the page, not the app. Renders null until a routed open
          publishes one, so every manually-opened session is unchanged. */}
      <ContactRouteToast />
    </div>
  );
}
