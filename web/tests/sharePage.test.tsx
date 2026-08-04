import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// The Web Share Target landing surface (/share). What matters here is not the
// form (that is /ingest's, reused wholesale) but the two guarantees around it:
// the shared payload reaches the form PREFILLED, and it is never dropped on the
// signed-out path — where the share sheet is already gone and the words would be
// unrecoverable.

const { routerQuery, replaceMock, session, ingest, submitMock } = vi.hoisted(() => ({
  routerQuery: { current: {} as Record<string, string | string[] | undefined> },
  replaceMock: vi.fn(),
  session: { current: { user: null as unknown, loading: false } },
  ingest: { current: {} as Record<string, unknown> },
  submitMock: vi.fn(),
}));

vi.mock('next/router', () => ({
  useRouter: () => ({ query: routerQuery.current, replace: replaceMock, push: vi.fn() }),
}));

vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => session.current,
}));

vi.mock('../lib/algernon/useIngest', () => ({
  useIngest: () => ingest.current,
}));

vi.mock('../lib/algernon/authClient', () => ({
  authApi: { logout: vi.fn(), login: vi.fn(), me: vi.fn() },
}));

vi.mock('../lib/algernon/sttClient', () => ({
  sttClient: { transcribe: vi.fn() },
}));

import SharePage from '../pages/share';
import { SHARE_SOURCE_FALLBACK, SHARE_STASH_KEY } from '../lib/algernon/shareTarget';

const OWNER = { name: 'Andrew', role: 'owner' };

beforeEach(() => {
  sessionStorage.clear();
  routerQuery.current = {};
  replaceMock.mockReset();
  submitMock.mockReset();
  session.current = { user: OWNER, loading: false };
  ingest.current = {
    targets: [{ name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] }],
    status: 'ready',
    error: null,
    result: null,
    unauthenticated: false,
    submit: submitMock,
    reset: vi.fn(),
  };
});

afterEach(() => {
  vi.clearAllMocks();
});

function field(testId: string): HTMLInputElement | HTMLTextAreaElement {
  return screen.getByTestId(testId) as HTMLInputElement | HTMLTextAreaElement;
}

describe('/share — a shared capture reaches the form prefilled', () => {
  it('maps title, text and url into the ingest fields', () => {
    routerQuery.current = {
      title: 'Tide tables',
      text: 'high water at 14:20',
      url: 'https://example.com/tides',
    };
    render(<SharePage />);
    expect(screen.getByTestId('ingest-form')).toBeTruthy();
    expect(field('ingest-title').value).toBe('Tide tables');
    expect(field('ingest-body').value).toBe('high water at 14:20');
    expect(field('ingest-source').value).toBe('https://example.com/tides');
  });

  it('a plain text share derives a title and the neutral source fallback', () => {
    routerQuery.current = { text: 'pick up the trailer hitch' };
    render(<SharePage />);
    expect(field('ingest-body').value).toBe('pick up the trailer hitch');
    expect(field('ingest-source').value).toBe(SHARE_SOURCE_FALLBACK);
    expect(field('ingest-title').value).toMatch(/^pick up the trailer hitch — shared \d{4}-\d{2}-\d{2} \d{4}$/);
  });

  it('does NOT auto-submit — the operator reviews and files it', () => {
    routerQuery.current = { text: 'something worth keeping' };
    render(<SharePage />);
    expect(submitMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('ingest-submit')).toBeTruthy();
  });

  it('the target picker is still a real choice (not silently defaulted away)', () => {
    routerQuery.current = { text: 'route me somewhere' };
    render(<SharePage />);
    expect(screen.getByTestId('ingest-form')).toBeTruthy();
    expect(screen.queryByTestId('share-nothing')).toBeNull();
  });
});

describe('/share — nothing is ever dropped', () => {
  it('parks the capture in sessionStorage on arrival, BEFORE any auth branch', () => {
    session.current = { user: null, loading: false };
    routerQuery.current = { text: 'words that must survive sign-in' };
    render(<SharePage />);
    const parked = JSON.parse(sessionStorage.getItem(SHARE_STASH_KEY) ?? 'null');
    expect(parked.body).toBe('words that must survive sign-in');
  });

  it('signed out: shows the held text rather than bouncing to /login and losing it', () => {
    session.current = { user: null, loading: false };
    routerQuery.current = { text: 'words that must survive sign-in' };
    render(<SharePage />);
    // The /ingest page redirects when signed out; this one must not, because it
    // is holding a payload the share sheet can no longer re-send.
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('share-held-preview').textContent).toBe(
      'words that must survive sign-in',
    );
  });

  it('signed out: the sign-in link carries a PAYLOAD-FREE restore path', () => {
    session.current = { user: null, loading: false };
    routerQuery.current = { text: 'has spaces in it' };
    render(<SharePage />);
    const href = screen.getByTestId('share-sign-in').getAttribute('href');
    // The text rides sessionStorage, never the redirect — a next= holding it
    // would be downgraded to '/' by safeNextPath's whitespace rejection.
    expect(href).toBe('/login?next=%2Fshare%3Frestore%3D1');
    expect(href).not.toContain('has spaces');
  });

  it('after sign-in, the parked capture is restored with NO payload in the query', () => {
    sessionStorage.setItem(
      SHARE_STASH_KEY,
      JSON.stringify({ title: 'Parked', body: 'restored body', source: 'https://a.test' }),
    );
    routerQuery.current = { restore: '1' };
    render(<SharePage />);
    expect(field('ingest-body').value).toBe('restored body');
    expect(field('ingest-title').value).toBe('Parked');
  });

  it('the stash is cleared once the capture is in the form (no stale resurrection)', () => {
    sessionStorage.setItem(
      SHARE_STASH_KEY,
      JSON.stringify({ title: 'Parked', body: 'restored body', source: 'x' }),
    );
    routerQuery.current = { restore: '1' };
    render(<SharePage />);
    expect(sessionStorage.getItem(SHARE_STASH_KEY)).toBeNull();
  });
});

describe('/share — explicit states, never a blank pane', () => {
  it('a bare visit with nothing shared says so instead of showing an empty form', () => {
    routerQuery.current = {};
    render(<SharePage />);
    expect(screen.getByTestId('share-nothing')).toBeTruthy();
    expect(screen.queryByTestId('ingest-form')).toBeNull();
  });

  it('a non-owner session gets the owner-only state, not a form that would 403', () => {
    session.current = { user: { name: 'Guest', role: 'guest' }, loading: false };
    routerQuery.current = { text: 'not mine to file' };
    render(<SharePage />);
    expect(screen.getByTestId('share-owner-only')).toBeTruthy();
    expect(screen.queryByTestId('ingest-form')).toBeNull();
  });

  it('a non-owner share is still PARKED (a later owner sign-in can file it)', () => {
    session.current = { user: { name: 'Guest', role: 'guest' }, loading: false };
    routerQuery.current = { text: 'not mine to file' };
    render(<SharePage />);
    expect(sessionStorage.getItem(SHARE_STASH_KEY)).not.toBeNull();
  });

  it('a loading session shows an explicit line, not a blank pane', () => {
    session.current = { user: null, loading: true };
    render(<SharePage />);
    expect(screen.getByTestId('share-auth-gate')).toBeTruthy();
  });
});
