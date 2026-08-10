import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';

// #62 gate, WARN-1 — the WIRING pin.
//
// The reviewer deleted `useResumeRefetch(...)` from BOTH pages and the suite
// stayed 1,328 green. The hook's own 8 tests render no page, and feedBoard's
// change was fixture-shape only, so nothing observed that the pages actually
// CALL it. A feature that can be deleted without turning a test red is a
// feature the suite is not protecting — and this one was the incident's most
// visible symptom (an 11:43 screenshot of a deck rendered at 23:43).
//
// These mount the REAL page components and assert a REAL refetch, which is the
// only level at which the deletion is visible. Same lesson as #57's ingest form
// and its client_max_size threading pin: the value that never reaches the call
// site is the trap, so pin the call site.

const { mockList } = vi.hoisted(() => ({ mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
// `events` is required: a child of the home composer subscribes to route
// changes, and a router mock without it throws in the commit phase.
vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: vi.fn(), push: vi.fn(), query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
vi.mock('../lib/algernon/usePush', () => ({
  usePush: () => ({ status: 'unsupported', busy: false, enable: vi.fn(), disable: vi.fn() }),
}));

import FeedPage from '../pages/feed';
import HomePage from '../pages/index';

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

beforeEach(() => {
  mockList.mockReset();
  mockList.mockResolvedValue({ items: [], count: 0 });
  setVisibility('visible');
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each([
  ['the feed page', () => <FeedPage />],
  ['the home composer', () => <HomePage />],
])('%s refetches when the PWA resumes', (_label, renderPage) => {
  it('calls the feed again on visibilitychange', async () => {
    render(renderPage());
    await waitFor(() => expect(mockList).toHaveBeenCalled());
    const atMount = mockList.mock.calls.length;

    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await Promise.resolve();
    });

    await waitFor(() => expect(mockList.mock.calls.length).toBeGreaterThan(atMount));
  });

  it('calls the feed again on pageshow (Safari bfcache restore)', async () => {
    render(renderPage());
    await waitFor(() => expect(mockList).toHaveBeenCalled());
    const atMount = mockList.mock.calls.length;

    await act(async () => {
      window.dispatchEvent(new Event('pageshow'));
      await Promise.resolve();
    });

    await waitFor(() => expect(mockList.mock.calls.length).toBeGreaterThan(atMount));
  });

  it('does NOT refetch when the page merely went hidden', async () => {
    render(renderPage());
    await waitFor(() => expect(mockList).toHaveBeenCalled());
    const atMount = mockList.mock.calls.length;

    setVisibility('hidden');
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await Promise.resolve();
    });

    expect(mockList.mock.calls.length).toBe(atMount);
  });
});
