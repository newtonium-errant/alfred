import { expect, test } from '@playwright/test';
import { plantSessionCookies } from './authCookies';

// OPT-IN V3 barge-in smoke — NOT part of any gate. Proves barge-in end to end: a
// SECOND scripted utterance fires MID-PLAYOUT of the assistant's spoken reply, the
// server interrupts the tone and commits the barge as the next turn, and BOTH
// exchanges land in the thread (in V2 a final landing while speaking could never
// become a turn — that's the killer assert). The team lead runs it against the
// barge-enabled keyless harness. Separate from voice-tts.spec.ts (barge ON).
//
// PREREQS (all manual — this spec does NOT start them):
//   1. `npx playwright install chromium`
//   2. Backend up, in-WSL2: pipeline: assistant, fake STT + fake TTS,
//      web.voice.tts.enabled: true AND web.voice.tts.barge_in.enabled: true.
//      CRITICAL determinism precondition: the fake reply must be long enough that
//      turn 1's tone plays for ≥ ~2.5s, so the fake-STT's SECOND utterance (which
//      fires ~2.0s of mic audio after the first) deterministically lands MID-
//      PLAYOUT (a barge) rather than after the tone drains (a plain supersede).
//      FakeTTSProvider duration ≈ ms_per_char × chars (cap 5000ms) ⇒ reply ≥ ~80
//      chars. If the reply is short the run soft-passes WITHOUT exercising barge.
//   3. Next dev in-WSL2: `npm run dev`; .env.local NEXT_PUBLIC_VOICE_ENABLED=1 +
//      ALFRED_WEB_TRANSPORT_URL + ALFRED_WEB_PEER_TOKEN.
//   4. Run: VOICE_SMOKE_SESSION_TOKEN=<token> npm run smoke:voice:barge
//
// LATENCY (§1.11): this spec REPORTS a FE-observable barge-stop number — the wall
// clock from turn 1's 'Speaking…' pill appearing to it leaving Speaking (the audio
// cut). It is a corroborating signal; the AUTHORITATIVE ~0.7-1.6s perceived-stop
// envelope is the BACKEND's own measurement (the FE can't see the server-side
// moment the user started talking). WSL2 trap: Chromium INSIDE WSL2 (same netns as
// aiortc) or ICE never completes (microsoft/WSL#8783).

test('a mid-playout utterance barges in: tone stops early, both exchanges land', async ({
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

  // (a) Turn 1 becomes audible.
  await expect(status).toContainText('Speaking', { timeout: 20_000 });
  const tSpeakingStart = Date.now();

  // (b) With ZERO user interaction the scripted second utterance barges: the pill
  //     LEAVES 'Speaking' BEFORE the reply's natural drain — the audio-stops-early
  //     signal. Assert the DURABLE fact (pill no longer Speaking), NOT the transient
  //     'Thinking' window: with fake providers the Thinking phase is milliseconds and
  //     falls between Playwright polls (observed: 9 polls saw Speaking, 35 saw
  //     Listening, Thinking never sampled). Transient states on an instant-fake
  //     pipeline are unobservable by polling — assert durable outcomes only (the same
  //     lesson as the V1 dictation + V2 tts specs). not-Speaking fires on Thinking OR
  //     Listening, whichever the poll catches; the metric's meaning is unchanged
  //     (FE-observed playout-until-audio-cut).
  await expect(status).not.toContainText('Speaking', { timeout: 20_000 });
  const bargeStopMs = Date.now() - tSpeakingStart;
  // Reported gate metric (§1.11) — FE-observed playout-until-barge-cut.
  console.log(`[barge-smoke] FE-observed barge stop (Speaking→not-Speaking): ${bargeStopMs}ms`);

  // (c) The barge is ACCEPTED, not the V2 discard — the "Heard you — hold on"
  //     notice must NOT appear on the accept path.
  await expect(page.getByTestId('voice-discard-notice')).toHaveCount(0);

  // (d) Turn 2 gets its OWN 'Speaking…' phase — the full loop survived the
  //     interrupt (exercises the provider teardown / re-open path end to end).
  await expect(status).toContainText('Speaking', { timeout: 20_000 });

  // (e) THE KILLER ASSERT: BOTH exchanges durably persisted in the thread. In V2 a
  //     final landing mid-playout could never become a turn — here the barge did.
  const thread = page.getByTestId('chat-thread');
  await expect(thread.getByTestId('msg-user').nth(1)).toBeVisible({ timeout: 20_000 });
  await expect(thread.getByTestId('msg-assistant').nth(1)).toBeVisible({ timeout: 20_000 });

  // (f) Clean teardown.
  await page.getByTestId('voice-hangup').click();
  await expect(page.getByTestId('voice-start')).toBeVisible({ timeout: 10_000 });
});
