import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { authApi } from '../lib/algernon/authClient';
import { fetchAudio, fetchNarration, type NarrationResult, type PlayerAudioState } from '../lib/algernon/briefPlayer';
import { narrationSlides, slideAtFraction, slideDeepLink } from '../lib/algernon/player';
import { usePlayer } from '../components/player/usePlayer';
import { usePlayerAsk } from '../components/player/usePlayerAsk';
import { useMediaSession } from '../components/player/useMediaSession';
import { VoiceCapture } from '../components/chat/VoiceCapture';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle, title as titleClass } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The interruptible briefing player (C3b + C3c). Renders the narration as slides, plays
// the cached audio when available, and degrades to TEXT-ALONG when it isn't — never a
// broken play button. play / pause / ask / resume-at-position ride the usePlayer state
// machine (interruption is first-class); Media Session mirrors the controls to
// lock-screen/car. The ask surface (C3c) wires a real chat turn: mic (STT) or keyboard →
// a turn carrying the on-screen primer (brief_date + the PAUSED slide's section) → the
// assistant's answer as a text card → resume at the held instant.

export default function PlayerPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [narration, setNarration] = useState<NarrationResult | null>(null);
  const [audio, setAudio] = useState<PlayerAudioState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  // The ask surface's keyboard input (C3c) — always live; the STT "Use" prefills it.
  const [question, setQuestion] = useState('');
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
          // #52 — a 401 from THIS FEATURE's fetch is not proof the session is
          // dead. `/web/brief/*` validates the session token; `/feed/*` (which
          // fed the home page the operator just came from) validates only the
          // BFF's peer token. So one family can reject a session the other
          // never checked, and treating that as global session death silently
          // "logs out" a user the home page greeted by name seconds earlier.
          //
          // Ask the SESSION endpoint, which is the only authority on the
          // question. Redirect only if it agrees the session is gone.
          try {
            await authApi.me();
            if (!cancelled) {
              setError(
                "Your briefing couldn't be loaded, but you're still signed in. Try again, or open /brief to read it.",
              );
            }
          } catch (probe) {
            if (cancelled) return;
            if (probe instanceof ApiError && probe.status === 401) {
              onAuthExpired(); // genuinely signed out — the redirect is honest
            } else {
              // The probe itself failed (offline, 5xx). Absence of an answer is
              // not a "you are logged out" answer.
              setError('Could not reach the server. Check your connection and try again.');
            }
          }
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
  // The brief being played (for the ask primer) — present whenever slides are.
  const briefDate = useMemo(
    () => (narration && 'narration' in narration ? narration.narration.brief_date : null),
    [narration],
  );
  const player = usePlayer(slides);
  const playerAsk = usePlayerAsk({ onAuthExpired });
  const hasAudio = audio?.kind === 'audio';
  const { state, position, currentSlide, slideCount } = player;

  // A fresh ask surface each time it closes: clear the input once an answer lands.
  useEffect(() => {
    if (playerAsk.status === 'answered') setQuestion('');
  }, [playerAsk.status]);

  // Ask carries the on-screen primer — brief_date + the CURRENT (paused) slide's section,
  // not slide 0 (position is held across the asking state). Invalid primer ⟹ the backend
  // answers un-grounded (fail-soft), so an absent slide/date still sends a real question.
  const submitAsk = useCallback(async () => {
    const q = question.trim();
    if (!q || playerAsk.sending) return;
    await playerAsk.ask(q, { brief_date: briefDate ?? '', section_id: currentSlide?.sectionId ?? '' });
  }, [question, playerAsk, briefDate, currentSlide]);

  // Resume from the ask surface: clear the ask (superseding any in-flight turn) + the
  // input, then return the machine to the held instant.
  const onResume = useCallback(() => {
    playerAsk.reset();
    setQuestion('');
    player.resume();
  }, [playerAsk, player]);

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

            {/* The ask surface — FIRST-CLASS interruption (C3c): mic (STT) or keyboard →
                a real chat turn carrying the on-screen primer → the assistant's answer as
                a text card → resume at the held instant. The keyboard stays live even when
                STT fails (VoiceCapture shows its own honest "type it instead"). */}
            {state === 'asking' && (
              <div data-testid="player-ask" className="mt-3 rounded-xl border border-honeydew-300 bg-honeydew-50 p-4">
                <p className="text-sm font-semibold text-honeydew-700">
                  Paused{currentSlide ? ` — ask about ${currentSlide.title}` : ' — ask a question'}.
                </p>

                {/* Keyboard — ALWAYS live (the primary type path AND the STT-fail fallback). */}
                <textarea
                  data-testid="player-ask-input"
                  aria-label="Ask about this slide"
                  rows={2}
                  value={question}
                  disabled={playerAsk.sending}
                  onChange={(e) => setQuestion(e.target.value)}
                  className="mt-2 w-full rounded-lg border border-honeydew-300 bg-white px-3 py-2 text-sm text-honeydew-700 disabled:opacity-60"
                />
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="player-ask-send"
                    disabled={playerAsk.sending || question.trim().length === 0}
                    onClick={() => void submitAsk()}
                    className="rounded-lg border border-honeydew-600 bg-honeydew-600 px-4 py-2 text-xs font-bold uppercase tracking-wider text-cream disabled:opacity-40"
                  >
                    Ask
                  </button>
                  {playerAsk.sending && (
                    // Intentionally-left-blank: an explicit working signal, not a dead UI.
                    <span data-testid="player-ask-sending" className={`text-xs ${subtle}`}>
                      Asking…
                    </span>
                  )}
                </div>

                {/* Mic — reuses VoiceCapture (record → STT → editable review); Use prefills
                    the input above so the operator confirms the transcript before it sends. */}
                <div className="mt-3">
                  <VoiceCapture idPrefix="player-ask-voice" onTranscript={(t) => setQuestion(t)} disabled={playerAsk.sending} />
                </div>

                {/* Error — the keyboard above stays live to retry (honest, no dead mic). */}
                {playerAsk.error && (
                  <p role="alert" data-testid="player-ask-error" className="mt-2 text-sm text-danger">
                    {playerAsk.error}
                  </p>
                )}

                {/* The answer — a TEXT card in-player (answer-TTS is boarded, not built). */}
                {playerAsk.answer != null && (
                  <div data-testid="player-ask-answer" className="mt-3 rounded-lg border border-honeydew-200 bg-cream p-3 text-sm text-honeydew-700">
                    {playerAsk.answer}
                  </div>
                )}

                <button
                  type="button"
                  data-testid="player-resume"
                  onClick={onResume}
                  className="mt-3 rounded-lg border border-honeydew-400 px-4 py-2 text-xs font-bold uppercase tracking-wider text-honeydew-700"
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
