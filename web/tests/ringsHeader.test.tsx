import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// Pins the RingsHeader render: three tier rings (segments vs empty red circle),
// the tap-to-expand bucket panel, the disabled ✓ placeholder (no mutation path),
// row-tap evidence, the all-empty ILB caption, and the fetch/401 seam.
// Plain DOM assertions only (the suite runs without jest-dom — see vitest.setup).

const { mockList } = vi.hoisted(() => ({ mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));

import { RingsHeader } from '../components/feed/RingsHeader';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function slot(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'slot_suggestion:task/A.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Pay rent',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { tier: 1, name: 'Pay rent', surface_reason: 'due today' },
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  };
}

beforeEach(() => {
  mockList.mockReset();
  mockList.mockResolvedValue({ items: [], count: 0 });
});
afterEach(() => vi.restoreAllMocks());

describe('RingsHeader (controlled render)', () => {
  it('renders three rings; empty tiers draw the red empty circle', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1 } })]} />);
    expect(screen.queryByTestId('ring-1')).not.toBeNull();
    expect(screen.queryByTestId('ring-2')).not.toBeNull();
    expect(screen.queryByTestId('ring-3')).not.toBeNull();
    // Tier 1 has an item → no empty circle; tiers 2 & 3 are empty → red circle.
    expect(screen.queryByTestId('ring-empty-1')).toBeNull();
    expect(screen.queryByTestId('ring-empty-2')).not.toBeNull();
    expect(screen.queryByTestId('ring-empty-3')).not.toBeNull();
  });

  it('draws one arc segment per item in a bucket', () => {
    const { container } = render(
      <RingsHeader
        items={[
          slot({ id: 'a', evidence: { tier: 1 } }),
          slot({ id: 'b', evidence: { tier: 1 } }),
          slot({ id: 'c', evidence: { tier: 1 } }),
        ]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(3);
  });

  it('shows the all-empty ILB caption when nothing is in any ring', () => {
    render(<RingsHeader items={[]} />);
    expect(screen.queryByTestId('rings-empty')).not.toBeNull();
  });

  it('tapping a ring toggles its bucket panel', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1 } })]} />);
    expect(screen.queryByTestId('ring-panel-1')).toBeNull();
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-1')).not.toBeNull();
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-1')).toBeNull();
  });

  it('the panel lists items with a DISABLED ✓ (no mutation path), and a row-tap shows evidence', () => {
    render(<RingsHeader items={[slot({ id: 'a', evidence: { tier: 1, surface_reason: 'due today' } })]} />);
    fireEvent.click(screen.getByTestId('ring-1'));
    expect(screen.queryByTestId('ring-panel-item')).not.toBeNull();
    const complete = screen.getByTestId('ring-complete') as HTMLButtonElement;
    expect(complete.disabled).toBe(true);
    // Visually-honest disabled: the muted class is pinned WITH the disabled attr,
    // so un-disabling this (making it live) forces a conscious restyle too.
    expect(complete.className).toContain('opacity-50');
    // Evidence hidden until the row is tapped.
    expect(screen.queryByTestId('ring-item-evidence')).toBeNull();
    fireEvent.click(screen.getByTestId('ring-item-row'));
    expect(screen.queryByTestId('ring-item-evidence')).not.toBeNull();
    expect(screen.getByTestId('ring-item-evidence').textContent).toContain('due today');
  });

  it('an empty bucket panel shows its own ILB line', () => {
    render(<RingsHeader items={[]} />);
    fireEvent.click(screen.getByTestId('ring-2'));
    expect(screen.queryByTestId('ring-panel-empty')).not.toBeNull();
  });
});

describe('RingsHeader (fetch seam)', () => {
  it('fetches open slot_suggestion items on mount', async () => {
    mockList.mockResolvedValue({ items: [slot({ id: 'a', evidence: { tier: 1 } })], count: 1 });
    const { container } = render(<RingsHeader />);
    await waitFor(() => expect(container.querySelectorAll('[data-testid="ring-1"] path')).toHaveLength(1));
    expect(mockList).toHaveBeenCalledWith({ kind: 'slot_suggestion', state: 'open' });
  });

  it('bubbles a 401 to onAuthExpired instead of showing an error', async () => {
    mockList.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const onAuthExpired = vi.fn();
    render(<RingsHeader onAuthExpired={onAuthExpired} />);
    await waitFor(() => expect(onAuthExpired).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('rings-error')).toBeNull();
  });

  it('shows an error banner on a non-401 failure', async () => {
    mockList.mockRejectedValue(new ApiError(502, 'feed_upstream_unavailable'));
    render(<RingsHeader />);
    await waitFor(() => expect(screen.queryByTestId('rings-error')).not.toBeNull());
  });

  it('skips the fetch entirely when items are supplied (controlled)', () => {
    render(<RingsHeader items={[]} />);
    expect(mockList).not.toHaveBeenCalled();
  });
});
