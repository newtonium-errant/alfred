import { describe, expect, it } from 'vitest';
import {
  CONTACT_RULE_ORDER,
  CONTACT_SURFACES,
  SURFACE_PATHS,
  evaluateRoute,
  isContactSurface,
  type DayState,
} from '../lib/algernon/contactRouter';

// The C4 rule set, evaluated. Pure — no router, no fetch, no rendered tree.
//
// The spec's four rungs in priority order, of which three are armed in v1:
//   2. unresolved notification → feed
//   3. first contact after gap → brief
//   4. default                → chat
// Rule 1 (resume pending capture) is unarmed: the server never lists it in
// `armed_rules`, and these tests pin that it stays unroutable.

function state(overrides: Partial<DayState> = {}): DayState {
  return {
    last_session_ended: '2026-08-13T00:00:00+00:00',
    time_since_last_session_hours: 1,
    brief_read_today: true,
    unresolved_flagged_notifications: 0,
    first_unresolved_notification_id: null,
    last_active_surface: 'chat',
    rule_order: [...CONTACT_RULE_ORDER],
    armed_rules: ['unresolved_notification', 'first_contact_after_gap', 'default'],
    unarmed_rules: { resume_pending_capture: 'needs open_capture_pending' },
    adopted_defaults: {},
    levers: { gap_hours_new_day: 6, brief_read_decay_hours: 12 },
    levers_source: 'preference_record',
    configured: true,
    ...overrides,
  };
}

describe('the fail-safe is staying put', () => {
  it('does not route when there is no state at all', () => {
    expect(evaluateRoute(null)).toBeNull();
  });

  it('does not route on an unconfigured instance', () => {
    expect(evaluateRoute(state({ configured: false }))).toBeNull();
  });

  it('does not route when no rule is armed', () => {
    expect(evaluateRoute(state({ armed_rules: [] }))).toBeNull();
  });

  it('DOES route on the same payload once armed — the positive control', () => {
    // Without this, every assertion above passes against a broken evaluator.
    expect(evaluateRoute(state())?.rule).toBe('default');
  });
});

describe('rule 2 — unresolved notification', () => {
  it('opens the feed when something is unresolved', () => {
    const d = evaluateRoute(state({ unresolved_flagged_notifications: 2 }));
    expect(d?.rule).toBe('unresolved_notification');
    expect(d?.surface).toBe('feed');
    expect(d?.path).toBe('/feed');
  });

  it('carries the notification to scroll to', () => {
    const d = evaluateRoute(state({
      unresolved_flagged_notifications: 1,
      first_unresolved_notification_id: 'n-42',
    }));
    expect(d?.scrollTo).toBe('n-42');
  });

  it('omits scrollTo when the tray gave no id', () => {
    const d = evaluateRoute(state({ unresolved_flagged_notifications: 1 }));
    expect(d?.scrollTo).toBeUndefined();
  });

  it('outranks rule 3 even when the gap rule would also fire', () => {
    const d = evaluateRoute(state({
      unresolved_flagged_notifications: 1,
      time_since_last_session_hours: 40,
      brief_read_today: false,
    }));
    expect(d?.rule).toBe('unresolved_notification');
  });
});

describe('rule 3 — first contact after a gap', () => {
  it('opens the brief past the gap with the brief unread', () => {
    const d = evaluateRoute(state({
      time_since_last_session_hours: 9,
      brief_read_today: false,
    }));
    expect(d?.rule).toBe('first_contact_after_gap');
    expect(d?.path).toBe('/brief');
  });

  it('does not fire inside the gap', () => {
    const d = evaluateRoute(state({
      time_since_last_session_hours: 3,
      brief_read_today: false,
    }));
    expect(d?.rule).toBe('default');
  });

  it('does not fire when the brief was already read', () => {
    const d = evaluateRoute(state({
      time_since_last_session_hours: 40,
      brief_read_today: true,
    }));
    expect(d?.rule).toBe('default');
  });

  it('uses the SERVER lever, not a number of its own', () => {
    const seven = state({
      time_since_last_session_hours: 7,
      brief_read_today: false,
      levers: { gap_hours_new_day: 6, brief_read_decay_hours: 12 },
    });
    expect(evaluateRoute(seven)?.rule).toBe('first_contact_after_gap');
    // Same gap, a wider lever from the record — now inside the threshold.
    const wider = { ...seven, levers: { ...seven.levers, gap_hours_new_day: 9 } };
    expect(evaluateRoute(wider)?.rule).toBe('default');
  });

  it('treats a first-ever contact (null gap) as after the gap', () => {
    const d = evaluateRoute(state({
      time_since_last_session_hours: null,
      brief_read_today: false,
    }));
    expect(d?.rule).toBe('first_contact_after_gap');
  });
});

describe('rule 4 — the default', () => {
  it('opens chat when nothing else fires', () => {
    const d = evaluateRoute(state());
    expect(d?.rule).toBe('default');
    expect(d?.path).toBe('/chat');
  });
});

describe('rule 1 is unarmed, and unroutable', () => {
  it('is skipped even if a server wrongly armed it', () => {
    // The armed list is the gate, but the surface map is the belt: rule 1 maps
    // to no surface, so it cannot route even if the gate is wrong.
    const d = evaluateRoute(state({
      armed_rules: ['resume_pending_capture', 'default'],
    }));
    expect(d?.rule).toBe('default');
  });
});

describe('an adopted default wins over the rule', () => {
  it('replaces the surface the rule would have opened', () => {
    const d = evaluateRoute(state({ adopted_defaults: { default: 'deck' } }));
    expect(d?.rule).toBe('default');
    expect(d?.surface).toBe('deck');
    expect(d?.path).toBe('/deck');
    expect(d?.adopted).toBe(true);
  });

  it('is ignored when it names a surface this build cannot route to', () => {
    const d = evaluateRoute(state({ adopted_defaults: { default: 'hologram' } }));
    expect(d?.surface).toBe('chat');
    expect(d?.adopted).toBe(false);
  });

  it('drops the scroll target when it moves rule 2 off the feed', () => {
    const d = evaluateRoute(state({
      unresolved_flagged_notifications: 1,
      first_unresolved_notification_id: 'n-42',
      adopted_defaults: { unresolved_notification: 'chat' },
    }));
    expect(d?.surface).toBe('chat');
    expect(d?.scrollTo).toBeUndefined();
  });
});

describe('the server owns the priority order', () => {
  it('honours a reordering that arrives in the payload', () => {
    // Rule 3 promoted above rule 2: both fire, and the payload's order decides.
    const d = evaluateRoute(state({
      unresolved_flagged_notifications: 1,
      time_since_last_session_hours: 40,
      brief_read_today: false,
      rule_order: [
        'first_contact_after_gap',
        'unresolved_notification',
        'default',
      ],
    }));
    expect(d?.rule).toBe('first_contact_after_gap');
  });

  it('falls back to the local order when the payload omits it', () => {
    const d = evaluateRoute(state({
      rule_order: [],
      unresolved_flagged_notifications: 1,
    }));
    expect(d?.rule).toBe('unresolved_notification');
  });
});

describe('the vocabulary', () => {
  it('maps every surface to a path', () => {
    for (const s of CONTACT_SURFACES) {
      expect(SURFACE_PATHS[s]).toBeTruthy();
    }
  });

  it('narrows only known surfaces', () => {
    expect(isContactSurface('feed')).toBe(true);
    expect(isContactSurface('capture')).toBe(false);
    expect(isContactSurface(null)).toBe(false);
  });
});
