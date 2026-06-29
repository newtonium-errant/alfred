import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// vi.hoisted so the mock factory can reference the spy (factories are hoisted
// above other top-level code).
const { loginMock } = vi.hoisted(() => ({ loginMock: vi.fn() }));

vi.mock('next/router', () => ({
  useRouter: () => ({ query: {}, replace: vi.fn(), push: vi.fn() }),
}));

vi.mock('../lib/algernon/authClient', () => ({
  authApi: { login: loginMock },
}));

import LoginPage from '../pages/login';
import { ApiError } from '../lib/algernon/http';

afterEach(() => {
  loginMock.mockReset();
});

describe('LoginPage', () => {
  it('submits the email and shows the check-your-email confirmation', async () => {
    loginMock.mockResolvedValue({ status: 'sent' });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByTestId('email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('login-submit'));

    expect(loginMock).toHaveBeenCalledWith('andrew@example.com');
    expect(await screen.findByTestId('login-sent')).not.toBeNull();
  });

  it('surfaces a config error when email sign-in is not configured', async () => {
    loginMock.mockRejectedValue(new ApiError(503, 'email_not_configured'));
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByTestId('email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('login-submit'));

    const err = await screen.findByTestId('login-error');
    expect(err.textContent).toContain('configured');
    // The confirmation must NOT show when the send failed.
    expect(screen.queryByTestId('login-sent')).toBeNull();
  });
});
