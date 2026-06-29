import '../styles/globals.css';
import type { AppProps } from 'next/app';
import Head from 'next/head';
import { Nunito } from 'next/font/google';
import { useEffect } from 'react';

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
    </div>
  );
}
