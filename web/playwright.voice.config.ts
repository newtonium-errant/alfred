import { defineConfig } from '@playwright/test';
import path from 'path';

// OPT-IN voice echo smoke — DELIBERATELY OUTSIDE the test/typecheck/lint/build
// gates (it needs a running backend + real WebRTC). Run with `npm run smoke:voice`
// AFTER the prereqs in e2e/voice-echo.spec.ts are up. Chromium's fake-media flags
// feed the checked-in sine tone as the mic so the echo is deterministic; media
// stays inside WSL2 (same netns as aiortc) to dodge the Windows↔WSL2 UDP trap.
const TONE = path.join(__dirname, 'e2e', 'fixtures', 'tone.wav');

export default defineConfig({
  testDir: './e2e',
  testMatch: /voice-echo\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  reporter: 'list',
  use: {
    baseURL: process.env.VOICE_SMOKE_BASE_URL || 'http://127.0.0.1:3000',
    // Reduced motion so the honeydew pulse/celebration animations can't flake.
    contextOptions: { reducedMotion: 'reduce' },
    launchOptions: {
      args: [
        '--use-fake-device-for-media-stream',
        '--use-fake-ui-for-media-stream',
        `--use-file-for-fake-audio-capture=${TONE}`,
        '--autoplay-policy=no-user-gesture-required',
      ],
    },
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  // webServer is NOT auto-started — see the spec header for the manual prereqs.
});
