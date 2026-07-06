import { expect, test } from '@playwright/test';
import { plantSessionCookies } from './authCookies';

// OPT-IN V2 TTS talk-back smoke — NOT part of any gate. Proves the full loop plus
// AUDIBLE OUTPUT: mic audio → STT → reply → streaming TTS → PCM on the remote
// WebRTC track → browser playout. The team lead runs it against a keyless harness
// (FakeTTSProvider = a fixed 440Hz tone), then a live-ElevenLabs dev smoke for real
// speech. Separate from voice-dictation.spec.ts (different harness: tts on).
//
// PREREQS (all manual — this spec does NOT start them):
//   1. `npx playwright install chromium`   (browser binary, not pulled by npm ci)
//   2. Backend up WITH voice + ASSISTANT pipeline + TTS enabled, in-WSL2:
//        - config web.voice.enabled: true, pipeline: assistant
//        - stt provider: FAKE (FakeStreamProvider) + FakeAnthropicClient reply
//        - web.voice.tts.enabled: true, provider: fake (FakeTTSProvider — a fixed
//          audible 440Hz tone, ≥0.3 full-scale, ~1-2s/sentence so playout is
//          real-time samplable). NO ElevenLabs key needed for this smoke.
//        - `alfred up`
//   3. Next dev server in-WSL2: `npm run dev` (defaults to :3000)
//        - .env.local: NEXT_PUBLIC_VOICE_ENABLED=1, ALFRED_WEB_TRANSPORT_URL,
//          ALFRED_WEB_PEER_TOKEN.
//   4. Run WITH a minted session token (the spec skips without it):
//        VOICE_SMOKE_SESSION_TOKEN=<token> npm run smoke:voice:tts
//        (override host via VOICE_SMOKE_BASE_URL)
//
// Assertion is a DURABLE-WINDOW one (the V1 lesson): the fake tone plays over
// REAL-TIME playout (seconds), so we assert the 'Speaking…' pill appears (generous
// timeout — playout is seconds, unlike the ms-fast text race) THEN sample the
// remote track's audio energy over a multi-second window (peak-RMS via a WebAudio
// AnalyserNode — the exact precedent from voice-echo.spec.ts). WSL2 trap: run
// Chromium INSIDE WSL2 (same netns as aiortc) or ICE never completes
// (microsoft/WSL#8783).

test('a spoken reply plays audible TTS on the remote track and lands in the thread', async ({
  page,
  context,
  baseURL,
}) => {
  const token = process.env.VOICE_SMOKE_SESSION_TOKEN;
  test.skip(
    !token,
    'Set VOICE_SMOKE_SESSION_TOKEN (a minted dev session token) to run the TTS smoke.',
  );

  const url = baseURL || process.env.VOICE_SMOKE_BASE_URL || 'http://127.0.0.1:3000';
  await plantSessionCookies(context, url, token as string);

  await page.goto('/');

  const start = page.getByTestId('voice-start');
  await expect(start).toBeEnabled({ timeout: 15_000 });
  await start.click();

  await expect(page.getByTestId('voice-status')).toContainText('Listening', { timeout: 20_000 });

  // The 'Speaking…' pill appears when the first synthesized PCM starts playing back.
  // Generous timeout: playout is real-time paced (seconds), NOT the ms-fast text
  // race warned about in voice-dictation.spec.ts.
  await expect(page.getByTestId('voice-status')).toContainText('Speaking', { timeout: 20_000 });

  // Sample the remote track's audio ENERGY over a multi-second window while the
  // tone plays (durable-window, never instant). RMS probe duplicated from
  // voice-echo.spec.ts:83-110 (WebAudio AnalyserNode peak-RMS) — the proven
  // precedent for asserting non-silent audio survives the Opus round-trip.
  const rms = await page.evaluate(async () => {
    const el = document.querySelector('[data-testid="voice-audio"]') as HTMLAudioElement | null;
    const stream = el?.srcObject as MediaStream | null;
    if (!stream) return -1;
    const Ctx =
      (window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext })
        .AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return -1;
    const ctx = new Ctx();
    await ctx.resume();
    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    src.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    let peak = 0;
    const t0 = performance.now();
    while (performance.now() - t0 < 8000) {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      peak = Math.max(peak, Math.sqrt(sum / buf.length));
      await new Promise((r) => setTimeout(r, 50));
    }
    await ctx.close();
    return peak;
  });

  // Tolerant floor — non-silent synthesized speech (the echo-spec threshold).
  expect(rms).toBeGreaterThan(0.001);

  // DURABLE outcome — the exchange also lands in the persisted chat thread.
  const thread = page.getByTestId('chat-thread');
  await expect(thread.getByTestId('msg-user').last()).toBeVisible({ timeout: 20_000 });
  await expect(thread.getByTestId('msg-assistant').last()).toBeVisible({ timeout: 20_000 });

  await page.getByTestId('voice-hangup').click();
  await expect(page.getByTestId('voice-start')).toBeVisible({ timeout: 10_000 });
});
