import { createRequire } from 'node:module';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// THE FAB DOES NOT COVER THE PAGE'S PRIMARY ACTION.
//
// From an operator screenshot: on /chat at phone width the floating bug-report
// button sat ON TOP of the composer's send button. The page's primary action was
// under the button you reach for when the page is already misbehaving.
//
// IT IS NOT A CHAT BUG. The FAB is `fixed` and mounted by Layout on every authed
// surface, so it is out of flow and nothing below it knows it is there; /chat is
// simply the surface whose last row is a control rather than text. The fix is a
// RESERVATION on <main>, which is why the pins below drive Layout rather than the
// chat page.
//
// jsdom has no layout engine — `getBoundingClientRect` returns zeros for
// everything, so a bounding-box intersection test here would "pass" against a
// build with the overlap fully restored. That is the same wall tests/
// deckLayout.test.tsx hit for the deck's card stack, and this file takes the same
// answer: pin the RELATIONSHIP between the reservation and the footprint that
// makes it necessary, and reconcile the class LITERALS against the constants so
// the two halves cannot drift apart.

vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: vi.fn(), push: vi.fn(), asPath: '/chat', query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));
// The mocks /chat needs to mount at all. Same set tests/chatComposerFlag.test.tsx
// uses; they say nothing about layout, they just get the page past its bootstrap.
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ idPrefix }: { idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock`}>mock-stt</button>
  ),
  sttErrorMessage: () => 'Couldn’t transcribe that.',
}));

import { Layout } from '../components/Layout';
import ChatPage from '../pages/chat';
import {
  FAB_FOOTPRINT_PX,
  FAB_INSET_PX,
  FAB_POSITION_CLASS,
  FAB_SAFE_PAD_CLASS,
  FAB_SIZE_CLASS,
  FAB_SIZE_PX,
} from '../components/ReportBugFab';

// The REAL Tailwind theme, resolved from this project's own config — the only
// way to turn `pb-24` into a number without hand-copying the scale into the test
// and then trusting the copy.
const req = createRequire(import.meta.url);
const SPACING = req('tailwindcss/resolveConfig')(req('../tailwind.config.cjs')).theme
  .spacing as Record<string, string>;

/** `pb-24` / `bottom-4` / `h-12` → px, through the project's own spacing scale. */
function px(utility: string): number {
  const step = utility.replace(/^(?:sm:)?[a-z]+-/, '');
  const rem = SPACING[step];
  expect(rem, `no spacing step "${step}" for "${utility}"`).toBeDefined();
  return parseFloat(rem) * 16;
}

describe('the reservation clears the button', () => {
  it('reserves MORE than the FAB occupies, not merely as much', () => {
    // `>` rather than `>=`, and the difference is the bug. A reserve exactly
    // equal to the footprint clears the geometry and still lets the button sit on
    // the composer's focus ring and the send button's shadow, both of which paint
    // outside the element box. Edges touching is the state that was photographed.
    expect(px(FAB_SAFE_PAD_CLASS)).toBeGreaterThan(FAB_FOOTPRINT_PX);
  });

  it('computes the footprint from the LARGER breakpoint inset', () => {
    // The button is `bottom-4` on phones and `bottom-5` from `sm` up. A footprint
    // derived from the phone value is 4px short on every wider screen — the fix
    // would look right on the device it was reported from and stay wrong
    // everywhere else.
    const insets = [...FAB_POSITION_CLASS.matchAll(/(?:^|\s)((?:sm:)?bottom-\d+)/g)].map((m) => m[1]);
    expect(insets.length, 'positive control: the position literal still sets bottom insets').toBe(2);
    expect(FAB_INSET_PX).toBe(Math.max(...insets.map(px)));
    expect(FAB_FOOTPRINT_PX).toBe(FAB_INSET_PX + FAB_SIZE_PX);
  });

  it('takes its size arithmetic from the class that actually renders', () => {
    // The literals must stay written out — Tailwind's JIT scans source text, so a
    // class built from the constants would emit no CSS at all. That leaves two
    // sources of truth for one geometry; this reconciles them.
    const size = /(?:^|\s)(h-\d+)/.exec(FAB_SIZE_CLASS);
    expect(size, 'positive control: the size literal is still parseable').not.toBeNull();
    expect(px(size![1])).toBe(FAB_SIZE_PX);
    // …and the literals are what the button is actually wearing.
    const { unmount } = render(<Layout onSignOut={() => {}}>content</Layout>);
    const cls = screen.getByTestId('report-bug-fab').className;
    for (const token of [...FAB_POSITION_CLASS.split(' '), ...FAB_SIZE_CLASS.split(' ')]) {
      expect(cls.split(/\s+/)).toContain(token);
    }
    unmount();
  });
});

describe('the reservation is declared where the FAB is mounted', () => {
  function mainOf(container: HTMLElement): HTMLElement {
    const el = container.querySelector('main');
    expect(el, 'positive control: Layout still renders a <main>').not.toBeNull();
    return el as HTMLElement;
  }

  it('<main> carries it on a surface that HAS the button', () => {
    // THE MUTATION THIS PIN EXISTS FOR: drop `FAB_SAFE_PAD_CLASS` from Layout's
    // main and this goes red. That mutation is exactly "restore the overlap".
    const { container, unmount } = render(<Layout onSignOut={() => {}}>content</Layout>);
    expect(screen.getByTestId('report-bug-fab')).toBeTruthy();
    expect(mainOf(container).className.split(/\s+/)).toContain(FAB_SAFE_PAD_CLASS);
    unmount();
  });

  it('and NOT on a surface that has no button — the two read one expression', () => {
    // The other direction, and it is what makes the pin above mean "reserved
    // because the button is there" rather than "reserved always". A pin on the
    // presence alone stays green against an unconditional reserve, which would
    // put 96px of dead space under the sign-in screen.
    const { container, unmount } = render(
      <Layout showNav={false}>content</Layout>,
    );
    expect(screen.queryByTestId('report-bug-fab')).toBeNull();
    expect(mainOf(container).className.split(/\s+/)).not.toContain(FAB_SAFE_PAD_CLASS);
    unmount();
  });

  it('follows `showBugReport` when it disagrees with `showNav`', () => {
    // `showBugReport ?? showNav` is the real expression, and the two pins above
    // only ever vary `showNav` — so both stay green against a Layout that read
    // `showNav` alone and ignored the explicit override. This is the axis they
    // cannot see.
    const { container: off, unmount: unmountOff } = render(
      <Layout showBugReport={false}>content</Layout>,
    );
    expect(screen.queryByTestId('report-bug-fab')).toBeNull();
    expect(mainOf(off).className.split(/\s+/)).not.toContain(FAB_SAFE_PAD_CLASS);
    unmountOff();

    const { container: on, unmount: unmountOn } = render(
      <Layout showNav={false} showBugReport>content</Layout>,
    );
    expect(screen.getByTestId('report-bug-fab')).toBeTruthy();
    expect(mainOf(on).className.split(/\s+/)).toContain(FAB_SAFE_PAD_CLASS);
    unmountOn();
  });
});

describe('the reservation survives on the surface it was reported from', () => {
  it('/chat renders BOTH the send button and the FAB, with the reserve between them', async () => {
    // Driven through the REAL page rather than a hand-rendered Layout. The pins
    // above prove Layout reserves when asked; a page that stopped asking — by
    // wrapping its content in something other than Layout, say — would leave
    // every one of them green while the operator's screenshot came back.
    //
    // The two elements are queried by NAME because naming them is the claim:
    // this is the pair that was overlapping, and a pin that only checked the
    // padding class would stay green on a page that had lost the composer.
    (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async (url: string) => {
      const u = String(url);
      const body = u.startsWith('/api/chat/open')
        ? { session_key: 'sess-1' }
        : u.startsWith('/api/chat/notifications')
          ? { notifications: [], unread: 0 }
          : { targets: [], turns: [] };
      return { ok: true, status: 200, json: async () => body } as unknown as Response;
    });

    const { container, unmount } = render(<ChatPage />);
    await screen.findByTestId('report-bug-fab');
    // The composer is behind a build-time flag with two live spellings; either
    // one is the page's primary action and either one is what the FAB covered.
    await waitFor(() => {
      const send =
        screen.queryByTestId('composer-send') ?? screen.queryByTestId('unified-send');
      expect(send, 'positive control: /chat rendered a send control at all').not.toBeNull();
    });

    const main = container.querySelector('main');
    expect(main).not.toBeNull();
    expect(main!.className.split(/\s+/)).toContain(FAB_SAFE_PAD_CLASS);
    unmount();
  });
});
