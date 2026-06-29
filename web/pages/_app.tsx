import '../styles/globals.css';
import type { AppProps } from 'next/app';
import Head from 'next/head';
import { Nunito } from 'next/font/google';

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

// PWA shell (manifest, icons, service-worker registration) is Milestone 2; this
// M1 _app keeps only the font + base document head.
export default function App({ Component, pageProps }: AppProps) {
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
      </Head>
      <Component {...pageProps} />
    </div>
  );
}
