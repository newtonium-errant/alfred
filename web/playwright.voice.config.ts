import { defineConfig } from '@playwright/test';
import path from 'path';

// OPT-IN voice smokes — DELIBERATELY OUTSIDE the test/typecheck/lint/build gates
// (they need a running backend + real WebRTC). Four specs, run ONE AT A TIME
// against the matching backend config: `npm run smoke:voice` (echo),
// `npm run smoke:voice:dictation` (assistant), `npm run smoke:voice:tts`
// (assistant + fake TTS), `npm run smoke:voice:barge` (assistant + fake TTS +
// barge_in). Each pins an explicit spec file so they never all run against a single
// backend config. Chromium's fake-media flags feed the checked-in sine tone as the
// mic; media stays inside WSL2 (same netns as aiortc) to dodge the Windows↔WSL2 UDP
// trap.
const TONE = path.join(__dirname, 'e2e', 'fixtures', 'tone.wav');

export default defineConfig({
  testDir: './e2e',
  testMatch: /voice-(echo|dictation|tts|barge)\.spec\.ts/,
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
