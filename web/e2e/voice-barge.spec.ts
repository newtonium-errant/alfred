import { expect, test } from '@playwright/test';
import { plantSessionCookies } from './authCookies';

// OPT-IN V3 barge-in smoke — NOT part of any gate. Proves barge-in end to end:
// scripted utterances fire MID-PLAYOUT of the assistant's spoken replies, the server
// interrupts the tone and commits each barge as the NEXT turn, and every utterance
// lands in the thread as a real exchange (in V2 a final arriving while speaking could
// never become a turn — that's the killer assert). Team lead runs it against the
// barge-enabled keyless harness. Separate from voice-tts.spec.ts (barge ON).
//
// PREREQS (all manual — this spec does NOT start them):
//   1. `npx playwright install chromium`
//   2. Backend up, in-WSL2: pipeline: assistant, fake STT + fake TTS,
//      web.voice.tts.enabled: true AND web.voice.tts.barge_in.enabled: true.
//      DETERMINISM precondition: the fake reply must be long enough that a turn's
//      tone plays for ≥ ~2.5s so the fake-STT's NEXT utterance (~2.0s of mic audio
//      later) lands MID-PLAYOUT (a barge) not after the tone drains. Reference run:
//      ~12s natural tone/turn, barges confirmed ~1.5s in, turns_spoken=3.
//   3. Next dev in-WSL2: `npm run dev`; .env.local NEXT_PUBLIC_VOICE_ENABLED=1 +
//      ALFRED_WEB_TRANSPORT_URL + ALFRED_WEB_PEER_TOKEN.
//   4. Run: VOICE_SMOKE_SESSION_TOKEN=<token> npm run smoke:voice:barge
//
// WHY THIS SPEC ASSERTS ONLY THREAD + AGGREGATE-DURATION (no pill-transition
// semantics): on a CHAINED barge the speaking windows ABUT — speaking_done(T1) is
// followed ~instantly by speaking_started(T2) — so the 'Speaking…' pill NEVER leaves
// Speaking at a barge; it stays 'Speaking' CONTINUOUSLY across all barged turns and
// only leaves at the LAST turn's natural drain (reference run: ~13.8s continuous
// Speaking across 3 turns; Thinking/Listening between barges never materialize).
// Individual barge transitions are therefore FE-unobservable. The only honest FE
// observables are DURABLE: (1) the thread — each barged utterance became a real
// exchange; (2) the AGGREGATE 'Speaking' duration — far shorter than the sum of the
// natural tones because audio was genuinely flushed at each barge. (Same
// transient-region lesson as the V1 dictation + V2 tts specs, taken to its limit.)
// WSL2 trap: Chromium INSIDE WSL2 (same netns as aiortc) or ICE never completes
// (microsoft/WSL#8783).

test('chained mid-playout barges: each utterance becomes a real exchange; audio genuinely flushed', async ({
  page,
  context,
  baseURL,
}) => {
  const token = process.env.VOICE_SMOKE_SESSION_TOKEN;
  test.skip(!token, 'Set VOICE_SMOKE_SESSION_TOKEN (a minted dev session token) to run the barge smoke.');

  const url = baseURL || process.env.VOICE_SMOKE_BASE_URL || 'http://127.0.0.1:3000';
  await plantSessionCookies(context, url, token as string);
  await page.goto('/');

  const start = page.getByTestId('voice-start');
  await expect(start).toBeEnabled({ timeout: 15_000 });
  await start.click();

  const status = page.getByTestId('voice-status');
  await expect(status).toContainText('Listening', { timeout: 20_000 });

  // The assistant becomes audible (first turn's tone). The pill then stays 'Speaking'
  // continuously across the chained barges until the LAST turn drains naturally.
  await expect(status).toContainText('Speaking', { timeout: 25_000 });
  const tSpeakingStart = Date.now();

  // AGGREGATE audio-cut metric (durable): wait for the pill to FINALLY leave Speaking
  // (the last turn's natural drain). The total continuous 'Speaking' span must be far
  // under the uncut sum of natural tones — proof audio was genuinely flushed at each
  // barge, WITHOUT asserting where any individual transition blinked. Reference: ~13.8s
  // observed vs ~33s if nothing were cut (3 × ~11-12s). Bound generously at 2× one tone.
  await expect(status).not.toContainText('Speaking', { timeout: 30_000 });
  const aggregateSpeakingMs = Date.now() - tSpeakingStart;
  console.log(`[barge-smoke] aggregate continuous 'Speaking' span: ${aggregateSpeakingMs}ms`);
  expect(aggregateSpeakingMs).toBeLessThan(20_000); // < 2× ~12s natural tone ⇒ audio was cut

  // The barges are ACCEPTED (not the V2 discard path) — the "Heard you — hold on"
  // notice must NOT be showing.
  await expect(page.getByTestId('voice-discard-notice')).toHaveCount(0);

  // THE KILLER ASSERT (primary): ≥3 user/assistant exchange pairs durably persisted —
  // the barging utterances (utt2 AND utt3) each became a real turn. In V2 a final
  // landing mid-playout could NEVER produce this. nth(2) ⇒ the third pair is present.
  const thread = page.getByTestId('chat-thread');
  await expect(thread.getByTestId('msg-user').nth(2)).toBeVisible({ timeout: 20_000 });
  await expect(thread.getByTestId('msg-assistant').nth(2)).toBeVisible({ timeout: 20_000 });

  // Clean teardown.
  await page.getByTestId('voice-hangup').click();
  await expect(page.getByTestId('voice-start')).toBeVisible({ timeout: 10_000 });
});
