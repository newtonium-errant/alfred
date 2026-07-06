import { expect, test } from '@playwright/test';
import { plantSessionCookies } from './authCookies';

// OPT-IN V1 dictation smoke — NOT part of any gate. Proves the full streaming
// path: mic audio → WebRTC → server STT → text reply → thread adoption, over the
// REAL browser datachannel + decode/resample path. The team lead runs it against
// a keyless harness at integration.
//
// PREREQS (all manual — this spec does NOT start them):
//   1. `npx playwright install chromium`   (browser binary, not pulled by npm ci)
//   2. Backend up WITH voice enabled + the ASSISTANT pipeline, in-WSL2:
//        - config web.voice.enabled: true, pipeline: assistant
//        - stt provider: FAKE (FakeStreamProvider — scripted finals on feed-count,
//          NO Deepgram key) + a FakeAnthropicClient scripted reply
//        - `alfred up` (talker transport listening on its loopback port)
//   3. Next dev server in-WSL2: `npm run dev` (defaults to :3000)
//        - .env.local: NEXT_PUBLIC_VOICE_ENABLED=1, ALFRED_WEB_TRANSPORT_URL,
//          ALFRED_WEB_PEER_TOKEN.
//   4. Run WITH a minted session token (the spec skips without it):
//        VOICE_SMOKE_SESSION_TOKEN=<token> npm run smoke:voice:dictation
//        (override host via VOICE_SMOKE_BASE_URL)
//
// The fake-mic (--use-file-for-fake-audio-capture, playwright.voice.config.ts)
// feeds the checked-in tone continuously; the FakeStreamProvider fires its
// scripted utterances on feed-count (contract §1.7), so the exact transcript/reply
// STRINGS are harness-defined — this spec asserts STRUCTURE + streaming + thread
// adoption, not exact copy. WSL2 trap: run Chromium INSIDE WSL2 (same netns as
// aiortc) or ICE never completes (microsoft/WSL#8783).

test('a spoken utterance streams a transcript + reply and lands in the thread', async ({
  page,
  context,
  baseURL,
}) => {
  const token = process.env.VOICE_SMOKE_SESSION_TOKEN;
  test.skip(
    !token,
    'Set VOICE_SMOKE_SESSION_TOKEN (a minted dev session token) to run the dictation smoke.',
  );

  const url = baseURL || process.env.VOICE_SMOKE_BASE_URL || 'http://127.0.0.1:3000';
  await plantSessionCookies(context, url, token as string);

  await page.goto('/');

  // Voice start is gated on the chat session_key (bound at offer time), so it
  // enables only once the chat has booted.
  const start = page.getByTestId('voice-start');
  await expect(start).toBeEnabled({ timeout: 15_000 });
  await start.click();

  // Reaches live+listening; the assistant pipeline confirms dictation (state:ready),
  // so the echo-mode "dictation unavailable" notice must NOT appear.
  await expect(page.getByTestId('voice-status')).toContainText('Listening', { timeout: 20_000 });
  await expect(page.getByTestId('voice-dictation-unavailable')).toHaveCount(0);

  // The scripted utterance's final transcript appears (structure, not exact copy).
  const transcript = page.getByTestId('voice-transcript');
  await expect(transcript).toBeVisible({ timeout: 20_000 });
  await expect(transcript).not.toHaveText('');

  // The streamed reply appears in the panel...
  await expect(page.getByTestId('voice-reply')).toBeVisible({ timeout: 20_000 });

  // ...and after turn_final the exchange is adopted into the chat thread (a new
  // assistant bubble), which is the durable record of the turn.
  await expect(page.getByTestId('chat-thread').getByTestId('msg-assistant').last()).toBeVisible({
    timeout: 20_000,
  });

  await page.getByTestId('voice-hangup').click();
  await expect(page.getByTestId('voice-start')).toBeVisible({ timeout: 10_000 });
});
