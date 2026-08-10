import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

// #62 rider (operator-ruled 2026-08-07). The push policy is STRICT and stays
// strict: only override-list sender highs ring. The toggle used to promise
// "Algernon rings you when something needs a decision", which describes a far
// broader system than the one that exists.
//
// Why this is a trust bug and not a copy nit: a toggle that overstates its reach
// teaches the operator to expect rings that never come — and an operator who has
// learned the notifications are unreliable stops acting on the ones that ARE.
// That is the same currency #62's main defect spends.

const { mockUsePush } = vi.hoisted(() => ({ mockUsePush: vi.fn() }));
vi.mock('../lib/algernon/usePush', () => ({ usePush: mockUsePush }));

import { PushToggle } from '../components/PushToggle';

const OVERPROMISE = /rings you when something needs a decision/i;

function renderWith(status: string) {
  mockUsePush.mockReturnValue({
    status, busy: false, enable: vi.fn(), disable: vi.fn(),
  });
  return render(<PushToggle />);
}

afterEach(() => cleanup());

describe('the push toggle promises only what the policy delivers', () => {
  it('the ON copy names the override list, not "anything that needs a decision"', () => {
    renderWith('on');
    const text = screen.getByTestId('push-toggle').textContent ?? '';
    expect(text).toMatch(/override list/i);
    expect(text).not.toMatch(OVERPROMISE);
  });

  it('the OFF copy makes the same bounded promise', () => {
    // The off-state line is the one that SELLS the feature, so it is the one
    // most able to set a false expectation.
    renderWith('off');
    const text = screen.getByTestId('push-toggle').textContent ?? '';
    expect(text).toMatch(/override list/i);
    expect(text).not.toMatch(/nudge when something needs you/i);
  });

  it('no rendered state re-introduces the overpromise', () => {
    for (const status of ['on', 'off', 'denied', 'error']) {
      renderWith(status);
      expect(screen.getByTestId('push-toggle').textContent ?? '').not.toMatch(OVERPROMISE);
      cleanup();
    }
  });
});
