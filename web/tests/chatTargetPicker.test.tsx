import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ChatTargetPicker } from '../components/chat/ChatTargetPicker';
import type { ChatTarget } from '../lib/algernon/types';

const TARGETS: ChatTarget[] = [
  { name: 'Salem', label: 'Salem', home: true },
  { name: 'KALLE', label: 'KAL-LE', home: false },
  { name: 'VERA', label: 'VERA', home: false },
];

describe('ChatTargetPicker', () => {
  it('renders nothing when only the home instance is configured', () => {
    const { container } = render(
      <ChatTargetPicker
        targets={[{ name: 'Salem', label: 'Salem', home: true }]}
        instance="Salem"
        onInstanceChange={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId('chat-target')).toBeNull();
  });

  it('renders an option per target and shows the active one', () => {
    render(
      <ChatTargetPicker targets={TARGETS} instance="KALLE" onInstanceChange={() => {}} />,
    );
    const select = screen.getByTestId('chat-target') as HTMLSelectElement;
    expect(select.value).toBe('KALLE');
    expect(screen.getByText('KAL-LE')).not.toBeNull();
    expect(screen.getByText('VERA')).not.toBeNull();
  });

  it('fires onInstanceChange with the chosen instance name', () => {
    const onChange = vi.fn();
    render(<ChatTargetPicker targets={TARGETS} instance="Salem" onInstanceChange={onChange} />);
    fireEvent.change(screen.getByTestId('chat-target'), { target: { value: 'VERA' } });
    expect(onChange).toHaveBeenCalledWith('VERA');
  });

  it('disables the select when disabled', () => {
    render(
      <ChatTargetPicker targets={TARGETS} instance="Salem" onInstanceChange={() => {}} disabled />,
    );
    expect((screen.getByTestId('chat-target') as HTMLSelectElement).disabled).toBe(true);
  });
});
