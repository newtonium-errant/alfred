import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Layout } from '../components/Layout';

// The Layout's two visual identities (interface-reimagine arc).
//
// `warm` is the honeydew theme every existing surface has always worn;
// `console` is the ratified Phase B identity. They are deliberately ONE
// component with a class table rather than two components, and these pins exist
// to keep that true, because the failure mode of a fork is not a crash — it is
// the console copy quietly losing an affordance (the sign-out, the mobile
// overflow, the unread badge) that nobody notices until an operator cannot sign
// out on the one surface they use daily.
//
// So the load-bearing assertion here is the SAMENESS one: every nav affordance
// must be present on both surfaces. The identity assertions are the cheap half.

vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), pathname: '/', query: {} }),
}));

/** Every interactive affordance the shell owes a signed-in operator. */
const NAV_AFFORDANCES = [
  'nav-chat',
  'nav-brief',
  'nav-deck',
  'nav-feed',
  'nav-ingest',
  'nav-scans',
  'nav-signout',
  'nav-menu-button',
];

describe('Layout surface identities', () => {
  it('offers exactly the same affordances on both surfaces', () => {
    // The anti-fork pin. Rendered separately and compared as sets, so a
    // console skin that dropped Sign out — or a warm skin that dropped it —
    // fails here rather than in an operator's hands.
    const seen: Record<string, string[]> = {};
    for (const surface of ['warm', 'console'] as const) {
      const { unmount } = render(
        <Layout onSignOut={() => {}} unreadCount={3} surface={surface}>
          <p>content</p>
        </Layout>,
      );
      seen[surface] = NAV_AFFORDANCES.filter((id) => screen.queryByTestId(id) !== null);
      // The badge is conditional on a non-zero count, which is supplied above.
      expect(screen.queryByTestId('nav-unread-badge')).not.toBeNull();
      unmount();
    }
    // Positive control on the denominator: this must have FOUND the
    // affordances, not compared two empty lists.
    expect(seen.warm).toEqual(NAV_AFFORDANCES);
    expect(seen.console).toEqual(seen.warm);
  });

  it('renders the warm identity by default, with no console markers', () => {
    const { container } = render(
      <Layout onSignOut={() => {}}>
        <p>content</p>
      </Layout>,
    );
    // Default-unchanged is what makes this layer safe to land: every existing
    // surface passes no `surface` prop at all.
    expect(container.querySelector('[data-surface="warm"]')).not.toBeNull();
    expect(container.querySelector('[data-surface="console"]')).toBeNull();
    expect(screen.queryByTestId('console-elbow')).toBeNull();
  });

  it('marks the console surface so the stylesheet can find it', () => {
    const { container } = render(
      <Layout onSignOut={() => {}} surface="console">
        <p>content</p>
      </Layout>,
    );
    // `data-surface="console"` is the hook console.css keys the ground, the
    // overscroll colour and the focus ring off. Without the attribute the whole
    // identity silently reverts to the warm page wash with console-coloured
    // text on it — legible enough in jsdom, unreadable on a real screen.
    const root = container.querySelector('[data-surface="console"]');
    expect(root).not.toBeNull();
    expect(root?.className).toContain('bg-console-hull');
    expect(screen.queryByTestId('console-elbow')).not.toBeNull();
  });

  it('keeps the elbow out of the accessibility tree', () => {
    render(
      <Layout onSignOut={() => {}} surface="console">
        <p>content</p>
      </Layout>,
    );
    // It is a corner block. It names nothing and does nothing, so announcing it
    // would be pure noise on a screen reader.
    expect(screen.getByTestId('console-elbow').getAttribute('aria-hidden')).toBe('true');
  });

  it('never draws the unread badge in the negative role', () => {
    // Parity with the warm surface's own rule ("calm pill — never danger-red,
    // which is reserved for true system errors"). On the console the badge is
    // caution; what it must never be is the reject colour, which would read as
    // "something failed" rather than "there is something for you".
    render(
      <Layout onSignOut={() => {}} unreadCount={2} surface="console">
        <p>content</p>
      </Layout>,
    );
    const badge = screen.getByTestId('nav-unread-badge');
    expect(badge.className).toContain('bg-caution');
    expect(badge.className).not.toContain('negative');
  });
});
