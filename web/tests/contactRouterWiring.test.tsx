import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// THE CALL-SITE PIN. Everything else about C4 can be green while the router is
// dead code: `contactRouter.test.ts` proves the rules decide, `useContactRouter
// .test.ts` proves the hook navigates, and both would stay green if nothing in
// the app ever CALLED the hook — exactly the shape `resumeRefetchWiring.test.tsx`
// exists for, and the shape that BLOCKed a snooze feature at gate for having a
// live write side and a dead read side.
//
// Two wirings, two failure modes:
//   1. `pages/index.tsx` calls the hook       → without it, nothing ever routes.
//   2. `pages/_app.tsx` renders the affordance → without it, every routed open
//      is unoverridable, and the correction signal the whole self-correcting
//      loop feeds on is never captured.
//
// Both are driven here through the REAL modules, not asserted about their source.

const {
  mockState, mockContact, mockOverride, mockList, mockReplace,
} = vi.hoisted(() => ({
  mockState: vi.fn(),
  mockContact: vi.fn(),
  mockOverride: vi.fn(),
  mockList: vi.fn(),
  mockReplace: vi.fn(),
}));

vi.mock('../lib/algernon/dayClient', () => ({
  dayApi: { state: mockState, contact: mockContact, override: mockOverride },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
// A FRESH object per call, deliberately: Next's own router happens to be stable
// across renders, and a hook that depends on that identity works in production
// while failing here. Mocking it the unstable way is what makes this pin able to
// catch the dependency-identity bug it caught.
vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: vi.fn(),
    asPath: '/',
    query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));
// `_app` calls next/font at module scope, which needs the Next build pipeline.
vi.mock('next/font/google', () => ({
  Nunito: () => ({ variable: 'font-nunito-test', className: 'nunito' }),
}));

import { CONTACT_RULE_ORDER, type DayState } from '../lib/algernon/contactRouter';
import { __resetContactRouteToast } from '../lib/algernon/useContactRouter';
import HomePage from '../pages/index';

function dayState(overrides: Partial<DayState> = {}): DayState {
  return {
    last_session_ended: null,
    time_since_last_session_hours: 1,
    brief_read_today: true,
    unresolved_flagged_notifications: 0,
    first_unresolved_notification_id: null,
    last_active_surface: '',
    rule_order: [...CONTACT_RULE_ORDER],
    armed_rules: ['unresolved_notification', 'first_contact_after_gap', 'default'],
    unarmed_rules: {},
    adopted_defaults: {},
    levers: { gap_hours_new_day: 6, brief_read_decay_hours: 12 },
    levers_source: 'defaults',
    configured: true,
    ...overrides,
  };
}

beforeEach(() => {
  __resetContactRouteToast();
  mockState.mockReset().mockResolvedValue(dayState());
  mockContact.mockReset().mockResolvedValue({ contact_id: 'c-1', recorded: true });
  mockOverride.mockReset().mockResolvedValue({ recorded: true, patterns_surfaced: 0 });
  mockList.mockReset().mockResolvedValue({ items: [], count: 0 });
  mockReplace.mockReset().mockResolvedValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the landing page actually runs the router', () => {
  it('reads the day state on open', async () => {
    render(<HomePage />);
    await waitFor(() => expect(mockState).toHaveBeenCalledTimes(1));
  });

  it('navigates to the decided surface', async () => {
    mockState.mockResolvedValue(dayState({ unresolved_flagged_notifications: 2 }));
    render(<HomePage />);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/feed'));
  });

  it('records the contact, so the router can learn from this open', async () => {
    render(<HomePage />);
    await waitFor(() =>
      expect(mockContact).toHaveBeenCalledWith('default', 'chat'),
    );
  });

  it('stays put when the instance is unconfigured', async () => {
    mockState.mockResolvedValue(dayState({ configured: false }));
    render(<HomePage />);
    await waitFor(() => expect(mockState).toHaveBeenCalled());
    expect(mockReplace).not.toHaveBeenCalled();
  });
});

describe('the app shell actually renders the override affordance', () => {
  it('shows the toast on the destination after a routed open', async () => {
    // Reproduces the real sequence: the decision is made by the page, and the
    // affordance is rendered by the shell that outlives it.
    const App = (await import('../pages/_app')).default;
    render(<App Component={HomePage} pageProps={{}} router={{} as never} />);
    await waitFor(() =>
      expect(screen.getByTestId('contact-route-toast')).toBeTruthy(),
    );
  });

  it('renders no affordance when nothing routed', async () => {
    mockState.mockResolvedValue(dayState({ configured: false }));
    const App = (await import('../pages/_app')).default;
    render(<App Component={HomePage} pageProps={{}} router={{} as never} />);
    await waitFor(() => expect(mockState).toHaveBeenCalled());
    expect(screen.queryByTestId('contact-route-toast')).toBeNull();
  });
});
