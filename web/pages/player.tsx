import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { authApi } from '../lib/algernon/authClient';
import { fetchAudio, fetchNarration, type NarrationResult, type PlayerAudioState } from '../lib/algernon/briefPlayer';
import { narrationSlides, slideAtFraction, slideDeepLink } from '../lib/algernon/player';
import { usePlayer } from '../components/player/usePlayer';
import { useMediaSession } from '../components/player/useMediaSession';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle, title as titleClass } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The interruptible briefing player (C3b). Renders the narration as slides, plays the
// cached audio when available, and degrades to TEXT-ALONG when it isn't — never a broken
// play button. play / pause / ask / resume-at-position ride the usePlayer state machine
// (interruption is first-class); Media Session mirrors the controls to lock-screen/car.
// The ask surface is a clearly-scoped STUB here (C3c wires STT + the chat primer).

export default function PlayerPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [narration, setNarration] = useState<NarrationResult | null>(null);
  const [audio, setAudio] = useState<PlayerAudioState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/player')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      try {
        const [n, a] = await Promise.all([fetchNarration(), fetchAudio()]);
        if (cancelled) {
          if (a.kind === 'audio') URL.revokeObjectURL(a.url); // don't leak on a raced unmount
          return;
        }
        setNarration(n);
        setAudio(a);
        if (a.kind === 'audio') objectUrlRef.current = a.url;
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          onAuthExpired();
          return;
        }
        setError('Could not load the player. Try refreshing.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed, onAuthExpired]);

  // Revoke the audio object URL when the player unmounts.
  useEffect(
    () => () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    },
    [],
  );

  const slides = useMemo(
    () => (narration && 'narration' in narration ? narrationSlides(narration.narration) : []),
    [narration],
  );
  const player = usePlayer(slides);
  const hasAudio = audio?.kind === 'audio';
  const { state, position, currentSlide, slideCount } = player;

  // Mirror the state machine onto the <audio> element (the machine is the source of
  // truth; asking/paused both pause the audio, so resume continues from the held instant).
  useEffect(() => {
    const el = audioRef.current;
    if (!el || !hasAudio) return;
    // Guarded: an autoplay-blocked play() rejects, and jsdom's play/pause throw
    // "not implemented" — neither must break the state machine.
    try {
      if (state === 'playing') {
        const p = el.play() as Promise<void> | undefined;
        if (p && typeof p.catch === 'function') p.catch(() => undefined);
      } else {
        el.pause();
      }
    } catch {
      /* audio element can't act right now — the state machine stays the truth */
    }
  }, [state, hasAudio]);

  useMediaSession({
    title: currentSlide?.title || 'Briefing',
    playing: state === 'playing',
    onPlay: player.play,
    onPause: player.pause,
    onNext: player.next,
    onPrev: player.prev,
  });

  const onTimeUpdate = useCallback(() => {
    const el = audioRef.current;
    if (!el || !el.duration) return;
    player.setOffset(el.currentTime);
    const idx = slideAtFraction(slides, el.currentTime / el.duration);
    if (idx !== position.slideIndex) player.seekToSlide(idx);
  }, [player, slides, position.slideIndex]);

  // A manual slide jump also seeks the audio to that slide's start (word_count share).
  const goToSlide = useCallback(
    (idx: number) => {
      player.seekToSlide(idx);
      const el = audioRef.current;
      if (el && hasAudio && el.duration) {
        const total = slides.reduce((n, s) => n + Math.max(0, s.wordCount), 0);
        let acc = 0;
        for (let i = 0; i < idx && i < slides.length; i++) acc += Math.max(0, slides[i].wordCount);
        el.currentTime = total > 0 ? (acc / total) * el.duration : 0;
      }
    },
    [player, slides, hasAudio],
  );

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* best-effort */
    }
    router.replace('/login');
  }, [router]);

  if (!authed) {
    return (
      <>
        <Head>
          <title>Briefing · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  const loaded = narration != null && audio != null && !error;
  const narrationState = narration && 'state' in narration ? narration.state : null;

  return (
    <>
      <Head>
        <title>Briefing · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <h1 className={display}>Your briefing</h1>

        {error && (
          <div role="alert" data-testid="player-error" className="mt-6 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        {!loaded && !error && (
          <p data-testid="player-loading" className={`mt-6 ${subtle}`}>
            Loading your briefing…
          </p>
        )}

        {/* ILB: no brief spooled → offer the deck (not an error). */}
        {loaded && narrationState === 'no_brief' && (
          <div data-testid="player-no-brief" className="mt-6 rounded-xl border border-honeydew-200 bg-cream p-4 shadow-soft">
            <p className={titleClass}>No brief today.</p>
            <p className={`mt-1 ${subtle}`}>Nothing to play yet — decisions are on the deck.</p>
            <Link href="/deck" data-testid="player-no-brief-link" className="mt-3 inline-block font-semibold text-honeydew-700 underline underline-offset-2">
              Open the deck →
            </Link>
          </div>
        )}

        {/* ILB: brief exists but its narration/audio is unavailable → offer the brief page. */}
        {loaded && narrationState === 'narration_unavailable' && (
          <div data-testid="player-narration-unavailable" className="mt-6 rounded-xl border border-honeydew-200 bg-cream p-4 shadow-soft">
            <p className={titleClass}>Brief exists — audio unavailable.</p>
            <p className={`mt-1 ${subtle}`}>The narration isn&rsquo;t ready, but the brief itself is.</p>
            <Link href="/brief" data-testid="player-narration-link" className="mt-3 inline-block font-semibold text-honeydew-700 underline underline-offset-2">
              Read the brief →
            </Link>
          </div>
        )}

        {/* ILB: a brief with no speakable segments (empty narration) → nothing to play. */}
        {loaded && narrationState === null && slideCount === 0 && (
          <div data-testid="player-empty" className="mt-6 rounded-xl border border-honeydew-200 bg-cream p-4 shadow-soft">
            <p className={titleClass}>Nothing to play.</p>
            <p className={`mt-1 ${subtle}`}>Today&rsquo;s brief has nothing to narrate.</p>
            <Link href="/brief" className="mt-3 inline-block font-semibold text-honeydew-700 underline underline-offset-2">
              Read the brief →
            </Link>
          </div>
        )}

        {loaded && slideCount > 0 && (
          <section data-testid="player" className="mt-4">
            {/* Text-along / degraded-audio banner — honest, never a broken play button. */}
            {!hasAudio && (
              <p data-testid="player-textalong" className={`mb-3 rounded-lg bg-honeydew-50 px-3 py-2 text-xs ${subtle}`}>
                {audio?.kind === 'tts_not_configured'
                  ? 'Audio isn’t configured — read along below.'
                  : 'Audio is unavailable right now — read along below.'}
              </p>
            )}

            {/* Slide progress dots. */}
            <div className="mb-3 flex items-center gap-1.5" aria-hidden data-testid="player-progress">
              {slides.map((s) => (
                <span
                  key={s.sectionId}
                  className={`h-1.5 rounded-full transition-all ${s.index === position.slideIndex ? 'w-6 bg-honeydew-700' : 'w-1.5 bg-honeydew-300'}`}
                />
              ))}
            </div>

            {/* The current slide — its element deep-links to the real surface. */}
            {currentSlide && (
              <div data-testid="player-slide" data-section={currentSlide.sectionId} className="rounded-2xl border border-honeydew-200 bg-cream p-5 shadow-card">
                <p className="text-[10px] font-bold uppercase tracking-wider text-honeydew-500">
                  Slide {position.slideIndex + 1} / {slideCount}
                </p>
                <h2 className="mt-1 text-lg font-extrabold leading-snug text-honeydew-700">{currentSlide.title}</h2>
                <p className="mt-2 break-words text-sm text-honeydew-600">{currentSlide.text}</p>
                <Link
                  href={slideDeepLink(currentSlide.sectionId)}
                  data-testid="player-slide-link"
                  className="mt-3 inline-block text-xs font-semibold text-honeydew-600 underline underline-offset-2"
                >
                  Open →
                </Link>
              </div>
            )}

            {/* The ask surface — FIRST-CLASS interruption. C3c wires STT + the chat primer;
                v1 shows the surface + the honest stub so the state is real, not a stop. */}
            {state === 'asking' && (
              <div data-testid="player-ask" className="mt-3 rounded-xl border border-honeydew-300 bg-honeydew-50 p-4">
                <p className="text-sm font-semibold text-honeydew-700">Paused — ask about this slide.</p>
                <p data-testid="player-ask-stub" className={`mt-1 text-xs ${subtle}`}>
                  Voice &amp; chat answers arrive with the next update. Resume when you&rsquo;re ready.
                </p>
                <button
                  type="button"
                  data-testid="player-resume"
                  onClick={player.resume}
                  className="mt-3 rounded-lg border border-honeydew-600 bg-honeydew-600 px-4 py-2 text-xs font-bold uppercase tracking-wider text-cream"
                >
                  Resume
                </button>
              </div>
            )}

            {/* Controls — off the state machine (one seam; Media Session mirrors them). */}
            {state !== 'asking' && (
              <div className="mt-4 flex items-center justify-center gap-2.5">
                <button
                  type="button"
                  data-testid="player-prev"
                  aria-label="Previous slide"
                  disabled={position.slideIndex === 0}
                  onClick={() => goToSlide(Math.max(0, position.slideIndex - 1))}
                  className="flex h-12 w-12 items-center justify-center rounded-full border-[1.5px] border-honeydew-400 text-honeydew-600 disabled:opacity-30"
                >
                  ‹
                </button>
                {state === 'playing' ? (
                  <button
                    type="button"
                    data-testid="player-pause"
                    aria-label="Pause"
                    onClick={player.pause}
                    className="flex h-14 w-14 items-center justify-center rounded-full border-[1.5px] border-honeydew-600 text-lg text-honeydew-700"
                  >
                    ‖
                  </button>
                ) : (
                  <button
                    type="button"
                    data-testid="player-play"
                    aria-label={state === 'paused' ? 'Resume' : 'Play'}
                    onClick={state === 'paused' ? player.resume : player.play}
                    className="flex h-14 w-14 items-center justify-center rounded-full border-[1.5px] border-honeydew-600 text-lg text-honeydew-700"
                  >
                    ▶
                  </button>
                )}
                <button
                  type="button"
                  data-testid="player-ask-open"
                  aria-label="Pause and ask"
                  disabled={state === 'idle'}
                  onClick={player.ask}
                  className="flex h-12 w-12 items-center justify-center rounded-full border-[1.5px] border-status-progress-fg text-status-progress-fg disabled:opacity-30"
                >
                  ?
                </button>
                <button
                  type="button"
                  data-testid="player-next"
                  aria-label="Next slide"
                  disabled={position.slideIndex >= slideCount - 1}
                  onClick={() => goToSlide(Math.min(slideCount - 1, position.slideIndex + 1))}
                  className="flex h-12 w-12 items-center justify-center rounded-full border-[1.5px] border-honeydew-400 text-honeydew-600 disabled:opacity-30"
                >
                  ›
                </button>
              </div>
            )}

            {hasAudio && audio?.kind === 'audio' && (
              // eslint-disable-next-line jsx-a11y/media-has-caption -- narration audio; the slide text IS the caption
              <audio
                ref={audioRef}
                data-testid="player-audio"
                src={audio.url}
                onTimeUpdate={onTimeUpdate}
                onEnded={player.ended}
                preload="auto"
              />
            )}
          </section>
        )}
      </Layout>
    </>
  );
}
