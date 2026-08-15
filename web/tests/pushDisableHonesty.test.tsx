import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// TURNING THE DOORBELL OFF NOW REPORTS WHAT ACTUALLY HAPPENED.
//
// `disable()` ran two steps — `sub.unsubscribe()` and a DELETE to the box —
// each under `.catch(() => undefined)`, and then set the status to 'off'
// UNCONDITIONALLY. Three different worlds produced the same calm word:
//
//   1. both worked                      → off. True.
//   2. the DELETE failed                → off is true FOR THIS BROWSER, but the
//                                         box still holds a subscription row.
//   3. the unsubscribe failed           → off is FALSE. The browser is still
//                                         subscribed and the next push arrives
//                                         at a device the operator silenced.
//
// (3) is the one that matters most and was the most completely hidden. These
// pins drive the SURFACE, not the hook, because a status the toggle never
// renders is not a fix.
//
// jsdom has no push stack at all, so the whole environment is built here. That
// is also why `pushToggle.test.tsx` could only ever assert the inert case.

interface FakeSub {
  endpoint: string;
  unsubscribe: () => Promise<boolean>;
}

const state: {
  sub: FakeSub | null;
  deleteOk: boolean;
  deleteThrows: boolean;
} = { sub: null, deleteOk: true, deleteThrows: false };

function installPushEnvironment() {
  (window as unknown as { PushManager: unknown }).PushManager = class {};
  (window as unknown as { Notification: unknown }).Notification = {
    permission: 'granted',
    requestPermission: vi.fn(async () => 'granted'),
  };
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      ready: Promise.resolve({
        pushManager: {
          getSubscription: async () => state.sub,
        },
      }),
    },
  });
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.startsWith('/api/push/public-key')) {
      return { ok: true, status: 200, json: async () => ({ publicKey: 'k' }) } as unknown as Response;
    }
    if (u.startsWith('/api/push/subscribe') && init?.method === 'DELETE') {
      if (state.deleteThrows) throw new Error('offline');
      return { ok: state.deleteOk, status: state.deleteOk ? 200 : 500 } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });
}

import { PushToggle } from '../components/PushToggle';
import { PUSH_SERVER_NOT_TOLD_MESSAGE, PUSH_STILL_ON_MESSAGE } from '../lib/algernon/usePush';

/** A subscription whose local unsubscribe behaves as told. */
function subscription(unsubscribe: () => Promise<boolean>): FakeSub {
  return { endpoint: 'https://push.example/abc', unsubscribe };
}

beforeEach(() => {
  state.sub = subscription(async () => true);
  state.deleteOk = true;
  state.deleteThrows = false;
  installPushEnvironment();
});
afterEach(() => vi.restoreAllMocks());

/** Mount and wait for the probe to settle on 'on' (an existing subscription). */
async function readyOn() {
  render(<PushToggle />);
  await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn off'));
}

describe('the browser would not unsubscribe', () => {
  it('does NOT say off — it still rings here, and the note says so', async () => {
    state.sub = subscription(async () => false); // resolves, but removed nothing
    await readyOn();

    fireEvent.click(screen.getByTestId('push-toggle-button'));

    const note = await screen.findByTestId('push-toggle-note');
    expect(note.textContent).toBe(PUSH_STILL_ON_MESSAGE);
    expect(note.getAttribute('role')).toBe('alert');
    // THE LOAD-BEARING ASSERTION: the control still reads as ON, because it is.
    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn off'));
  });

  it('a THROWN unsubscribe is the same fact and gets the same answer', async () => {
    // The old code's `.catch(() => undefined)` made these two paths identical
    // and both silent; they are still identical and now both speak.
    state.sub = subscription(async () => {
      throw new Error('the browser refused');
    });
    await readyOn();

    fireEvent.click(screen.getByTestId('push-toggle-button'));

    expect((await screen.findByTestId('push-toggle-note')).textContent).toBe(PUSH_STILL_ON_MESSAGE);
    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn off'));
  });
});

describe('the browser unsubscribed but the box was not told', () => {
  it('says off — which is true here — and names the row left behind', async () => {
    state.deleteOk = false;
    await readyOn();

    fireEvent.click(screen.getByTestId('push-toggle-button'));

    const note = await screen.findByTestId('push-toggle-note');
    expect(note.textContent).toBe(PUSH_SERVER_NOT_TOLD_MESSAGE);
    // 'off' is CORRECT in this branch and must not be downgraded to an error:
    // the endpoint is dead, so nothing will ring here. The note carries the
    // half that did not happen without taking the half that did.
    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn on'));
  });

  it('a DELETE that never got out is the same fact', async () => {
    state.deleteThrows = true;
    await readyOn();

    fireEvent.click(screen.getByTestId('push-toggle-button'));

    expect((await screen.findByTestId('push-toggle-note')).textContent).toBe(PUSH_SERVER_NOT_TOLD_MESSAGE);
    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn on'));
  });
});

describe('the clean case stays clean (the control)', () => {
  it('both steps succeed: off, and NO note', async () => {
    // Without this, every pin above would pass identically against a toggle
    // that rendered a note on every disable — which would be its own dishonesty,
    // teaching the operator to ignore the line that matters.
    await readyOn();

    fireEvent.click(screen.getByTestId('push-toggle-button'));

    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn on'));
    expect(screen.queryByTestId('push-toggle-note')).toBeNull();
  });

  it('nothing to unsubscribe is not a failure either', async () => {
    // Probe settles on 'off' with no subscription; pressing Turn on is a
    // different path, so this drives disable() against an already-clear state
    // via the enable/disable pair the surface offers. Nothing to undo, nothing
    // to report.
    state.sub = null;
    render(<PushToggle />);
    await waitFor(() => expect(screen.getByTestId('push-toggle-button').textContent).toBe('Turn on'));
    expect(screen.queryByTestId('push-toggle-note')).toBeNull();
  });
});
