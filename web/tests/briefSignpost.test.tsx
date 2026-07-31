import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// First-contact fix: a dismissible client-side signpost atop the brief render —
// reply-verbs work in Telegram; on the web, decisions live in the Deck/Feed. It
// is purely a page element (NEVER touches the brief record/markdown) and its
// dismissal persists via localStorage.

const { mockGetJson } = vi.hoisted(() => ({ mockGetJson: vi.fn() }));
vi.mock('../lib/algernon/http', () => ({
  getJson: mockGetJson,
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(status: number, code: string) {
      super(code);
      this.status = status;
      this.code = code;
    }
  },
}));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import BriefPage from '../pages/brief';

beforeEach(() => {
  mockGetJson.mockReset();
  mockGetJson.mockResolvedValue({ kind: 'brief', date: '2026-07-31', markdown: '# body' });
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
});
afterEach(() => vi.restoreAllMocks());

describe('BriefPage — signpost banner', () => {
  it('shows the signpost with Deck + Feed links, then dismiss persists it away', async () => {
    render(<BriefPage />);
    await waitFor(() => expect(screen.queryByTestId('brief-signpost')).not.toBeNull());
    const banner = screen.getByTestId('brief-signpost');
    expect(banner.textContent).toContain('Telegram');
    expect(banner.querySelector('a[href="/deck"]')).not.toBeNull();
    expect(banner.querySelector('a[href="/feed"]')).not.toBeNull();

    fireEvent.click(screen.getByTestId('brief-signpost-dismiss'));
    expect(screen.queryByTestId('brief-signpost')).toBeNull();
    expect(window.localStorage.getItem('algernon_brief_signpost')).toBe('dismissed');
  });

  it('stays dismissed on a fresh mount once localStorage records it', async () => {
    window.localStorage.setItem('algernon_brief_signpost', 'dismissed');
    render(<BriefPage />);
    // Let the mount effects (signpost check + fetch) settle, then confirm the
    // banner never appears.
    await waitFor(() => expect(mockGetJson).toHaveBeenCalled());
    expect(screen.queryByTestId('brief-signpost')).toBeNull();
  });
});
