import { describe, expect, it } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';
import { digestPayloadFor, pushTagFor } from '../lib/algernon/pushDigest';
import { pushPayloadFor } from '../lib/algernon/pushPayload';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// THE WORKER SIDE of the digest. `public/sw.js` is a static file vitest cannot
// import, so — same idiom as pushLink.test.ts — the push handler's own source is
// EXTRACTED and RUN against real payloads. Reading the file for the presence of
// a string would prove nothing about what it does with one.
//
// What is being pinned: the notification a payload actually produces. The digest
// needs a body the worker cannot compose from a kind ("digest · needs you" says
// less than nothing), and it needs the collapse key to stop being the deep link
// — every needs-you push resolves to `/deck`, so they all shared one tag and
// silently replaced each other in the tray.

const swSource = readFileSync(join(process.cwd(), 'public/sw.js'), 'utf8');

/** Run the worker's push handler over a payload; return the showNotification args. */
function renderPush(data: unknown): { title: string; options: Record<string, unknown> } {
  const handler = swSource.match(
    /self\.addEventListener\('push', \(event\) => \{([\s\S]*?)\n\}\);/,
  );
  if (!handler) throw new Error('could not extract the push handler from public/sw.js');
  const sanitize = swSource.match(/function sanitizeDeepLink\(url\)\s*\{([\s\S]*?)\n\}/);
  if (!sanitize) throw new Error('could not extract sanitizeDeepLink from public/sw.js');

  let captured: { title: string; options: Record<string, unknown> } | null = null;
  const self = {
    registration: {
      showNotification: (title: string, options: Record<string, unknown>) => {
        captured = { title, options };
      },
    },
  };
  const event = { data: { json: () => data }, waitUntil: (_p: unknown) => undefined };
  // eslint-disable-next-line no-new-func -- deliberately run the extracted SW source
  new Function(
    'self',
    'event',
    `function sanitizeDeepLink(url) {${sanitize[1]}\n}\n${handler[1]}`,
  )(self, event);
  if (!captured) throw new Error('the handler showed no notification');
  return captured;
}

function item(id: string, kind = 'slot_suggestion'): FeedItem {
  return withServedActions({
    id, kind, instance: 'salem', title: `Item ${id}`, mode: 'decide',
    attention: 'needs_you', evidence: {}, actions: [], state: 'open',
    created_at: '2026-08-16T00:00:00Z', acted_at: null, expires_at: null, source_ref: {},
  });
}

describe('sw.js push handler — the digest renders as one honest notification', () => {
  it('shows the digest TITLE and its supplied body, not "digest · needs you"', () => {
    const { title, options } = renderPush(digestPayloadFor([item('a'), item('b'), item('c', 'health')]));
    expect(title).toBe('2 tasks, 1 health need you');
    expect(options.body).toBe('Tap to open the deck');
  });

  it('collapses digests onto ONE rolling tag — the later replaces the earlier', () => {
    const first = renderPush(digestPayloadFor([item('a'), item('b')]));
    const second = renderPush(digestPayloadFor([item('a'), item('b'), item('c')]));
    expect(first.options.tag).toBe(second.options.tag);
  });

  it('an urgent item keeps its OWN tag — a digest cannot overwrite it', () => {
    // The regression this guards: the tag used to be the deep link, and every
    // needs-you push resolves to `/deck`. The ratified "urgent rings alone"
    // would have been undone one layer below the sender.
    const urgent = item('u1', 'email_urgent');
    const urgentPush = renderPush({ ...pushPayloadFor(urgent), tag: pushTagFor(urgent) });
    const digestPush = renderPush(digestPayloadFor([item('a'), item('b')]));
    expect(urgentPush.options.tag).not.toBe(digestPush.options.tag);
    expect(urgentPush.title).toBe('Item u1');
  });

  it('BACKWARD COMPATIBLE: a bodyless per-item payload still renders as before', () => {
    // The pre-existing contract, unchanged — the worker composes the body from
    // the kind and falls back to the deep link for the tag.
    const { title, options } = renderPush(pushPayloadFor(item('a')));
    expect(title).toBe('Item a');
    expect(options.body).toBe('slot_suggestion · needs you');
    expect(options.tag).toBe('/deck');
  });

  it('a malformed push still shows something, and never trusts an off-origin url', () => {
    const { title, options } = renderPush({ title: 123, url: '//evil.example', body: null });
    expect(title).toBe('Algernon');
    expect(options.body).toBe('needs you');
    expect((options.data as { url: string }).url).toBe('/feed');
  });
});
