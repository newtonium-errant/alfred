import { act, renderHook, waitFor } from '@testing-library/react';
import type { NextRouter } from 'next/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockState, mockContact, mockOverride } = vi.hoisted(() => ({
  mockState: vi.fn(),
  mockContact: vi.fn(),
  mockOverride: vi.fn(),
}));

vi.mock('../lib/algernon/dayClient', () => ({
  dayApi: { state: mockState, contact: mockContact, override: mockOverride },
}));

import { CONTACT_RULE_ORDER, type DayState } from '../lib/algernon/contactRouter';
import {
  __resetContactRouteToast,
  overrideChoices,
  useContactRouteToast,
  useContactRouter,
} from '../lib/algernon/useContactRouter';

// The C4 router's EFFECTS half: fetch → decide → record → navigate → publish the
// override affordance. The decision itself is pinned in contactRouter.test.ts;
// these are the effects, and the ones that matter are the ones that must NOT
// happen (no navigation on a failed read, never twice per mount).

function dayState(overrides: Partial<DayState> = {}): DayState {
  return {
    last_session_ended: null,
    time_since_last_session_hours: 1,
    brief_read_today: true,
    unresolved_flagged_notifications: 0,
    first_unresolved_notification_id: null,
    last_active_surface: '',
    minutes_since_last_contact: null,
    rule_order: [...CONTACT_RULE_ORDER],
    armed_rules: ['unresolved_notification', 'first_contact_after_gap', 'default'],
    unarmed_rules: {},
    adopted_defaults: {},
    levers: { gap_hours_new_day: 6, brief_read_decay_hours: 12, reroute_min_minutes: 30 },
    levers_source: 'defaults',
    configured: true,
    ...overrides,
  };
}

function fakeRouter(asPath = '/'): NextRouter {
  return {
    asPath,
    replace: vi.fn().mockResolvedValue(true),
    push: vi.fn(),
    query: {},
    events: { on: vi.fn(), off: vi.fn() },
  } as unknown as NextRouter;
}

beforeEach(() => {
  __resetContactRouteToast();
  mockState.mockReset();
  mockContact.mockReset();
  mockOverride.mockReset();
  mockContact.mockResolvedValue({ contact_id: 'c-1', recorded: true });
  mockOverride.mockResolvedValue({ recorded: true, patterns_surfaced: 0 });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the gate', () => {
  it('does nothing at all until authed', async () => {
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: false, router }));
    await Promise.resolve();
    expect(mockState).not.toHaveBeenCalled();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it('fires once per mount, not once per render', async () => {
    mockState.mockResolvedValue(dayState());
    const router = fakeRouter();
    const { rerender } = renderHook(
      (props: { enabled: boolean }) => useContactRouter({ ...props, router }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(mockState).toHaveBeenCalledTimes(1));
    rerender({ enabled: true });
    rerender({ enabled: true });
    await waitFor(() => expect(mockState).toHaveBeenCalledTimes(1));
  });
});

describe('a routed open', () => {
  it('navigates to the decided surface and records the contact', async () => {
    mockState.mockResolvedValue(dayState({ unresolved_flagged_notifications: 3 }));
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/feed'));
    expect(mockContact).toHaveBeenCalledWith('unresolved_notification', 'feed');
  });

  it('records BEFORE it navigates — the id is what an override attaches to', async () => {
    const order: string[] = [];
    mockState.mockResolvedValue(dayState());
    mockContact.mockImplementation(async () => {
      order.push('contact');
      return { contact_id: 'c-1', recorded: true };
    });
    const router = fakeRouter();
    (router.replace as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      order.push('replace');
      return true;
    });
    renderHook(() => useContactRouter({ enabled: true, router }));

    await waitFor(() => expect(order).toEqual(['contact', 'replace']));
  });

  it('publishes the override affordance carrying the contact id', async () => {
    mockState.mockResolvedValue(dayState());
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));
    const { result } = renderHook(() => useContactRouteToast());

    await waitFor(() => expect(result.current.toast).not.toBeNull());
    expect(result.current.toast?.contactId).toBe('c-1');
    expect(result.current.toast?.decision.surface).toBe('chat');
  });

  it('does not navigate when the decision is where we already are', async () => {
    mockState.mockResolvedValue(dayState({ adopted_defaults: { default: 'home' } }));
    const router = fakeRouter('/');
    const { result } = renderHook(() => useContactRouteToast());
    renderHook(() => useContactRouter({ enabled: true, router }));

    // The affordance still publishes: the router DID decide, and the operator
    // can still say it chose wrong.
    await waitFor(() => expect(result.current.toast).not.toBeNull());
    expect(router.replace).not.toHaveBeenCalled();
  });
});

describe('failure is staying put', () => {
  it('does not navigate when the state read fails', async () => {
    mockState.mockRejectedValue(new Error('offline'));
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));

    await waitFor(() => expect(mockState).toHaveBeenCalled());
    expect(router.replace).not.toHaveBeenCalled();
    expect(mockContact).not.toHaveBeenCalled();
  });

  it('does not navigate on an unconfigured instance', async () => {
    mockState.mockResolvedValue(dayState({ configured: false }));
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));

    await waitFor(() => expect(mockState).toHaveBeenCalled());
    expect(router.replace).not.toHaveBeenCalled();
  });

  it('still opens the surface when the contact log write fails', async () => {
    // The open is the operator's; only the learning is lost.
    mockState.mockResolvedValue(dayState());
    mockContact.mockRejectedValue(new Error('write failed'));
    const router = fakeRouter();
    const { result } = renderHook(() => useContactRouteToast());
    renderHook(() => useContactRouter({ enabled: true, router }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/chat'));
    expect(result.current.toast?.contactId).toBe('');
  });
});

describe('the override', () => {
  async function routed() {
    mockState.mockResolvedValue(dayState());
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));
    const { result } = renderHook(() => useContactRouteToast());
    await waitFor(() => expect(result.current.toast).not.toBeNull());
    return result;
  }

  it('records the surface the operator actually chose', async () => {
    const result = await routed();
    await act(async () => { await result.current.override('deck'); });
    expect(mockOverride).toHaveBeenCalledWith('c-1', 'deck');
  });

  it('clears the affordance so it cannot be tapped twice', async () => {
    const result = await routed();
    await act(async () => { await result.current.override('deck'); });
    expect(result.current.toast).toBeNull();
    await act(async () => { await result.current.override('feed'); });
    expect(mockOverride).toHaveBeenCalledTimes(1);
  });

  it('does not post when the contact could not be logged', async () => {
    mockState.mockResolvedValue(dayState());
    mockContact.mockRejectedValue(new Error('nope'));
    const router = fakeRouter();
    renderHook(() => useContactRouter({ enabled: true, router }));
    const { result } = renderHook(() => useContactRouteToast());
    await waitFor(() => expect(result.current.toast).not.toBeNull());

    await act(async () => { await result.current.override('deck'); });
    expect(mockOverride).not.toHaveBeenCalled();
  });

  it('swallows a failed override — it must never block the navigation', async () => {
    const result = await routed();
    mockOverride.mockRejectedValue(new Error('offline'));
    await act(async () => {
      await expect(result.current.override('deck')).resolves.toBeUndefined();
    });
  });

  it('dismiss clears without recording anything', async () => {
    const result = await routed();
    act(() => { result.current.dismiss(); });
    expect(result.current.toast).toBeNull();
    expect(mockOverride).not.toHaveBeenCalled();
  });
});

describe('the override choices', () => {
  it('never offers the surface you are already on', () => {
    expect(overrideChoices('feed')).not.toContain('feed');
    expect(overrideChoices('feed')).toContain('chat');
  });

  it('offers something for every routable decision', () => {
    for (const s of ['home', 'chat', 'feed', 'brief', 'deck']) {
      expect(overrideChoices(s).length).toBeGreaterThan(0);
    }
  });
});
