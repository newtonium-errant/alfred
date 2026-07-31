import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { PushToggle } from '../components/PushToggle';

// The toggle renders NOTHING when push is unavailable (jsdom has no PushManager),
// so an inert/unsupported deploy never shows a dead control.

afterEach(() => vi.restoreAllMocks());

describe('PushToggle', () => {
  it('renders nothing when the browser does not support push', async () => {
    render(<PushToggle />);
    // usePush resolves to 'unsupported' on mount → the component returns null.
    await waitFor(() => expect(screen.queryByTestId('push-toggle')).toBeNull());
  });
});
