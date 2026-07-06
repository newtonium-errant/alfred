import { expect, test } from '@playwright/test';

// OPT-IN V0 voice echo smoke — NOT part of any gate. The team lead runs it after
// integration. It proves audio actually round-trips: mic tone → WebRTC → aiortc
// echo → browser playback.
//
// PREREQS (all manual — this spec does NOT start them):
//   1. `npx playwright install chromium`   (browser binary, not pulled by npm ci)
//   2. Backend up WITH voice enabled, in-WSL2:
//        - config web.voice.enabled: true (pipeline: echo), aiortc installed
//        - `alfred up` (talker transport listening on its loopback port)
//   3. Next dev server in-WSL2: `npm run dev` (defaults to :3000)
//        - .env.local: NEXT_PUBLIC_VOICE_ENABLED=1, ALFRED_WEB_TRANSPORT_URL,
//          ALFRED_WEB_PEER_TOKEN, and ALFRED_WEB_DEV_SESSION_TOKEN (a dev session
//          token so the BFF is authenticated without the email round-trip).
//   4. Run: `npm run smoke:voice`   (override host via VOICE_SMOKE_BASE_URL)
//
// WSL2 trap: a Windows-side browser CANNOT complete media to aiortc inside WSL2
// (localhost forwarding is TCP-only, microsoft/WSL#8783). This smoke MUST run its
// Chromium INSIDE WSL2 (as configured) — same network namespace as aiortc.
//
// Assertion: after the panel reaches "Live", the hidden <audio> element's remote
// MediaStream carries non-silent audio — measured via a WebAudio AnalyserNode RMS
// over ~1.5s (a getStats() inbound-rtp bytesReceived check would need the pc
// exposed on window, which the shipped hook deliberately does not do; RMS on the
// received stream is a stronger end-to-end signal that echo audio truly flows).

test('mic tone echoes back through the live voice panel', async ({ page }) => {
  await page.goto('/');

  const start = page.getByTestId('voice-start');
  await expect(start).toBeEnabled({ timeout: 15_000 });
  await start.click();

  // The pill reaches Live once the pc connects (host-candidate-only, sub-second
  // in-netns; generous ceiling for CI-ish machines).
  await expect(page.getByTestId('voice-status')).toContainText('Live', { timeout: 20_000 });

  // Measure received-audio energy off the hidden <audio> element's srcObject.
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
    await ctx.resume(); // inside the (Live) gesture chain — satisfies autoplay
    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    src.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    let peak = 0;
    const t0 = performance.now();
    while (performance.now() - t0 < 1500) {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      peak = Math.max(peak, Math.sqrt(sum / buf.length));
      await new Promise((r) => setTimeout(r, 50));
    }
    await ctx.close();
    return peak;
  });

  // Tolerant floor — non-silent echo. Headless codecs/comfort-noise vary, so this
  // is intentionally low (the primary signal is "audio present, not digital zero").
  expect(rms).toBeGreaterThan(0.001);

  // Clean teardown returns the panel to idle.
  await page.getByTestId('voice-hangup').click();
  await expect(page.getByTestId('voice-start')).toBeVisible({ timeout: 10_000 });
});
