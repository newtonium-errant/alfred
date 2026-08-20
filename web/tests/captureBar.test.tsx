import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CaptureBar } from '../components/chat/CaptureBar';

// Structural pins for the unobtrusive capture toggle (R1): the capture-on
// indicator is PRESENT when active and ABSENT when not — both directions,
// because the indicator is the ILB signal that the assistant's silence is
// deliberate — plus the offer chip's copy and callback wiring.

const noop = () => {};

function bar(overrides: Partial<Parameters<typeof CaptureBar>[0]> = {}) {
  return (
    <CaptureBar
      active={false}
      busy={false}
      instanceLabel="Salem"
      offer={null}
      extracting={false}
      onToggle={noop}
      onExtract={noop}
      onDismissOffer={noop}
      {...overrides}
    />
  );
}

describe('CaptureBar', () => {
  it('capture ON: indicator present, names the quiet instance, button reads Stop', () => {
    render(bar({ active: true }));
    const indicator = screen.getByTestId('capture-indicator');
    expect(indicator.textContent).toContain('Capturing');
    expect(indicator.textContent).toContain('Salem is receiving, not replying');
    const toggle = screen.getByTestId('capture-toggle');
    expect(toggle.textContent).toBe('Stop capturing');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
  });

  it('capture OFF: indicator ABSENT, quiet Capture button present', () => {
    render(bar({ active: false }));
    expect(screen.queryByTestId('capture-indicator')).toBeNull();
    const toggle = screen.getByTestId('capture-toggle');
    expect(toggle.textContent).toBe('Capture');
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.getAttribute('disabled')).toBeNull();
  });

  it('toggle fires onToggle; busy/booting disable it', () => {
    const onToggle = vi.fn();
    const { rerender } = render(bar({ onToggle }));
    fireEvent.click(screen.getByTestId('capture-toggle'));
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(bar({ onToggle, busy: true }));
    expect(screen.getByTestId('capture-toggle').getAttribute('disabled')).not.toBeNull();
    rerender(bar({ onToggle, disabled: true }));
    expect(screen.getByTestId('capture-toggle').getAttribute('disabled')).not.toBeNull();
  });

  it('offer chip: count copy (plural + singular), accept/dismiss wired', () => {
    const onExtract = vi.fn();
    const onDismissOffer = vi.fn();
    const { rerender } = render(
      bar({ offer: { spanIndex: 0, turns: 3 }, onExtract, onDismissOffer }),
    );
    const chip = screen.getByTestId('capture-extract-offer');
    expect(chip.textContent).toContain('Captured 3 messages. Extract notes now?');
    fireEvent.click(screen.getByTestId('capture-extract-accept'));
    expect(onExtract).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('capture-extract-dismiss'));
    expect(onDismissOffer).toHaveBeenCalledTimes(1);

    rerender(bar({ offer: { spanIndex: 0, turns: 1 }, onExtract, onDismissOffer }));
    expect(
      screen.getByTestId('capture-extract-offer-text').textContent,
    ).toBe('Captured 1 message. Extract notes now?');
  });

  it('no offer → no chip (the quiet default)', () => {
    render(bar());
    expect(screen.queryByTestId('capture-extract-offer')).toBeNull();
  });

  it('extracting: chip says so and both buttons disable', () => {
    render(bar({ offer: { spanIndex: 1, turns: 2 }, extracting: true }));
    expect(
      screen.getByTestId('capture-extract-offer-text').textContent,
    ).toBe('Extracting…');
    expect(
      screen.getByTestId('capture-extract-accept').getAttribute('disabled'),
    ).not.toBeNull();
    expect(
      screen.getByTestId('capture-extract-dismiss').getAttribute('disabled'),
    ).not.toBeNull();
  });
});
