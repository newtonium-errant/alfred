import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// vi.hoisted so the mock factory can reference the spy (factories are hoisted
// above other top-level code). `routerQuery` is mutable so each test can drive
// router.query (e.g. ?next=…) before render.
const { loginMock, routerQuery } = vi.hoisted(() => ({
  loginMock: vi.fn(),
  routerQuery: { current: {} as Record<string, string | string[] | undefined> },
}));

vi.mock('next/router', () => ({
  useRouter: () => ({ query: routerQuery.current, replace: vi.fn(), push: vi.fn() }),
}));

vi.mock('../lib/algernon/authClient', () => ({
  authApi: { login: loginMock },
}));

import LoginPage from '../pages/login';
import { ApiError } from '../lib/algernon/http';

afterEach(() => {
  loginMock.mockReset();
  routerQuery.current = {};
});

describe('LoginPage', () => {
  it('submits the email and shows the check-your-email confirmation', async () => {
    loginMock.mockResolvedValue({ status: 'sent' });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByTestId('email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('login-submit'));

    // No ?next= in the query ⇒ the deep-link arg is undefined (the client drops it
    // from the body). The existing no-deep-link flow is unchanged.
    expect(loginMock).toHaveBeenCalledWith('andrew@example.com', undefined);
    expect(await screen.findByTestId('login-sent')).not.toBeNull();
  });

  it('passes ?next= through to authApi.login for the magic-link deep-link', async () => {
    routerQuery.current = { next: '/chat' };
    loginMock.mockResolvedValue({ status: 'sent' });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByTestId('email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('login-submit'));

    expect(loginMock).toHaveBeenCalledWith('andrew@example.com', '/chat');
    expect(await screen.findByTestId('login-sent')).not.toBeNull();
  });

  it('takes the first value when ?next= is repeated (string[])', async () => {
    routerQuery.current = { next: ['/chat', '/ignored'] };
    loginMock.mockResolvedValue({ status: 'sent' });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByTestId('email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('login-submit'));

    expect(loginMock).toHaveBeenCalledWith('andrew@example.com', '/chat');
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
