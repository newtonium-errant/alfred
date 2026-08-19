import { describe, expect, it } from 'vitest';
import { digestPayloadFor } from '../lib/algernon/pushDigest';
import { composeBatch } from '../lib/algernon/pushNotifier';
import { pushPayloadFor, type PushPayload } from '../lib/algernon/pushPayload';
import { trialPayloadFor } from '../lib/algernon/pushTrial';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// LOCK-SCREEN PRIVACY, over the WHOLE payload family.
//
// Why this file exists. `pushPayloadFor` had two guards — an exhaustive key set
// and a value check — and when the digest added a THIRD payload builder it
// inherited neither. It was safe by construction (pushDigest.ts reads no
// evidence) and undefended at exactly the spot the design calls dangerous: the
// `body` field, which pushPayload.ts's own docstring names as the privacy risk.
// A reviewer changed the digest body to an email subject and all 2566 tests
// passed.
//
// THE GUARD THAT ACTUALLY BITES IS THE VALUE CHECK, and that is worth stating
// because it is not the obvious one. A leak of the shape `body: evidence.subject`
// adds NO key: the exhaustive key set still sees five keys, and there is still no
// property called `evidence`. Both structural guards stay green. Only stuffing
// evidence with sentinels and searching the SERIALIZED payload for them catches
// it — which is how the incumbent pin was written, and the reason it works.
//
// Parametrized over every builder INCLUDING the incumbent: the guard written to
// close a hole in a new member has to cover the member that already existed, or
// the next addition repeats this.

/** Evidence stuffed with sentinels no payload may ever carry. */
const SECRETS = {
  sender: 'secret@person.com',
  subject: 'SENTINEL_SUBJECT',
  body: 'SENTINEL_BODY',
  body_preview: 'SENTINEL_PREVIEW',
  name: 'Jane Doe',
  gmail_url: 'https://mail.google.com/mail/u/0/#search/rfc822msgid:SENTINEL_MSGID',
  high_source: 'override',
};
const SENTINELS = [
  'SENTINEL_SUBJECT', 'SENTINEL_BODY', 'SENTINEL_PREVIEW', 'SENTINEL_MSGID',
  'secret@person.com', 'Jane Doe',
];

function item(id: string, kind = 'slot_suggestion'): FeedItem {
  return withServedActions({
    id,
    kind,
    instance: 'salem',
    // A producer-built title legitimately carries sender/subject; the rule is
    // about EVIDENCE reaching the lock screen, so the title stays innocuous here
    // and the sentinels live only where they are forbidden.
    title: `Item ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence: { ...SECRETS },
    actions: [],
    state: 'open',
    created_at: '2026-08-16T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

/** Every payload builder in the family, with the exact key set each may emit. */
const BUILDERS: Array<{
  name: string;
  keys: string[];
  build: () => PushPayload | { title: string; kind: string; url: string };
}> = [
  {
    name: 'pushPayloadFor (per-item — the incumbent)',
    keys: ['kind', 'title', 'url'],
    build: () => pushPayloadFor(item('a')),
  },
  {
    name: 'digestPayloadFor (the digest)',
    keys: ['body', 'kind', 'tag', 'title', 'url'],
    build: () => digestPayloadFor([item('a'), item('b'), item('c', 'health')]),
  },
  {
    name: 'trialPayloadFor (the delivery trial)',
    keys: ['kind', 'title', 'url'],
    build: () => trialPayloadFor('trial-d1-w1'),
  },
];

describe.each(BUILDERS)('lock-screen privacy: $name', ({ keys, build }) => {
  it('carries EXACTLY its declared keys — no field arrives unnoticed', () => {
    expect(Object.keys(build()).sort()).toEqual([...keys].sort());
  });

  it('has no `evidence` property', () => {
    expect(build()).not.toHaveProperty('evidence');
  });

  it('leaks NO evidence VALUE — the guard the structural ones cannot give', () => {
    // The one that catches `body: evidence.subject`. That leak adds no key and
    // no property named `evidence`, so it slips past both checks above.
    const serialized = JSON.stringify(build());
    for (const secret of SENTINELS) {
      expect(serialized).not.toContain(secret);
    }
  });
});

// COMPOSITION, not just the pieces. Production never calls a builder directly —
// it sends whatever `composeBatch` returns, and a field added anywhere along
// that path reaches the lock screen regardless of which builder is clean.
describe('lock-screen privacy: composeBatch (what production actually sends)', () => {
  const fresh = [item('a'), item('b'), item('u1', 'email_urgent')];
  const emitted = () => composeBatch(fresh, fresh);

  it('emits only known keys across every notification in a batch', () => {
    const allowed = new Set(['body', 'kind', 'tag', 'title', 'url']);
    const payloads = emitted();
    expect(payloads.length).toBeGreaterThan(0); // control: it composed something
    for (const p of payloads) {
      for (const key of Object.keys(p)) expect(allowed).toContain(key);
    }
  });

  it('leaks NO evidence value from ANY notification in a batch', () => {
    const serialized = JSON.stringify(emitted());
    for (const secret of SENTINELS) {
      expect(serialized).not.toContain(secret);
    }
    // Control: the batch really did carry the urgent item whose evidence is
    // stuffed — so the assertions above ran against a populated payload set and
    // not against an empty array.
    expect(serialized).toContain('Item u1');
  });
});
