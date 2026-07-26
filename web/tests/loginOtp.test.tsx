import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// The "Sign in with a code" flow on the login page (parity #23 — iOS PWA
// re-auth): toggle → request a code → enter the 6 digits → verify → client
// redirect to the (safeNextPath-guarded) deep-link. The cookie itself is set
// server-side by the BFF (covered in authOtpRoutes.test.ts) — this file locks
// the UI wiring and the graceful degradation when OTP is disabled.

const { loginMock, otpRequestMock, otpVerifyMock, replaceMock, routerQuery } = vi.hoisted(
  () => ({
    loginMock: vi.fn(),
    otpRequestMock: vi.fn(),
    otpVerifyMock: vi.fn(),
    replaceMock: vi.fn(),
    routerQuery: { current: {} as Record<string, string | string[] | undefined> },
  }),
);

vi.mock('next/router', () => ({
  useRouter: () => ({ query: routerQuery.current, replace: replaceMock, push: vi.fn() }),
}));

vi.mock('../lib/algernon/authClient', () => ({
  authApi: { login: loginMock, otpRequest: otpRequestMock, otpVerify: otpVerifyMock },
}));

import LoginPage from '../pages/login';
import { ApiError } from '../lib/algernon/http';

afterEach(() => {
  loginMock.mockReset();
  otpRequestMock.mockReset();
  otpVerifyMock.mockReset();
  replaceMock.mockReset();
  routerQuery.current = {};
});

async function driveToCodeStage(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId('otp-toggle'));
  await user.type(screen.getByTestId('otp-email-input'), 'andrew@example.com');
  await user.click(screen.getByTestId('otp-request-submit'));
  return screen.findByTestId('otp-code-stage');
}

describe('LoginPage — sign in with a code (OTP)', () => {
  it('magic-link form stays the default (keep-both: desktop path unchanged)', () => {
    render(<LoginPage />);
    expect(screen.getByTestId('email-input')).not.toBeNull();
    expect(screen.getByTestId('otp-toggle')).not.toBeNull();
    expect(screen.queryByTestId('otp-code-input')).toBeNull();
  });

  it('requests a code, verifies it, and redirects to the default /', async () => {
    otpRequestMock.mockResolvedValue({ ok: true });
    otpVerifyMock.mockResolvedValue({ ok: true });
    replaceMock.mockResolvedValue(true);
    const user = userEvent.setup();
    render(<LoginPage />);

    await driveToCodeStage(user);
    expect(otpRequestMock).toHaveBeenCalledWith('andrew@example.com');

    await user.type(screen.getByTestId('otp-code-input'), '123456');
    await user.click(screen.getByTestId('otp-verify-submit'));

    expect(otpVerifyMock).toHaveBeenCalledWith('andrew@example.com', '123456');
    expect(replaceMock).toHaveBeenCalledWith('/');
  });

  it('redirects to a safe ?next= deep-link after verify', async () => {
    routerQuery.current = { next: '/chat?instance=hypatia' };
    otpRequestMock.mockResolvedValue({ ok: true });
    otpVerifyMock.mockResolvedValue({ ok: true });
    replaceMock.mockResolvedValue(true);
    const user = userEvent.setup();
    render(<LoginPage />);

    await driveToCodeStage(user);
    await user.type(screen.getByTestId('otp-code-input'), '123456');
    await user.click(screen.getByTestId('otp-verify-submit'));

    expect(replaceMock).toHaveBeenCalledWith('/chat?instance=hypatia');
  });

  it('guards an open-redirect ?next= down to / (safeNextPath)', async () => {
    routerQuery.current = { next: 'https://evil.com' };
    otpRequestMock.mockResolvedValue({ ok: true });
    otpVerifyMock.mockResolvedValue({ ok: true });
    replaceMock.mockResolvedValue(true);
    const user = userEvent.setup();
    render(<LoginPage />);

    await driveToCodeStage(user);
    await user.type(screen.getByTestId('otp-code-input'), '123456');
    await user.click(screen.getByTestId('otp-verify-submit'));

    expect(replaceMock).toHaveBeenCalledWith('/');
  });

  it('shows the uniform error on a bad/expired code and does not redirect', async () => {
    otpRequestMock.mockResolvedValue({ ok: true });
    otpVerifyMock.mockRejectedValue(new ApiError(401, 'invalid_or_expired'));
    const user = userEvent.setup();
    render(<LoginPage />);

    await driveToCodeStage(user);
    await user.type(screen.getByTestId('otp-code-input'), '123456');
    await user.click(screen.getByTestId('otp-verify-submit'));

    const err = await screen.findByTestId('otp-error');
    expect(err.textContent).toContain('incorrect or has expired');
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('degrades gracefully when the instance has OTP disabled', async () => {
    otpRequestMock.mockRejectedValue(new ApiError(404, 'otp_disabled'));
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByTestId('otp-toggle'));
    await user.type(screen.getByTestId('otp-email-input'), 'andrew@example.com');
    await user.click(screen.getByTestId('otp-request-submit'));

    const err = await screen.findByTestId('otp-error');
    expect(err.textContent).toContain('isn’t enabled');
    // Still on the request stage — no code input appeared.
    expect(screen.queryByTestId('otp-code-input')).toBeNull();
  });

  it('can switch back to the magic-link form', async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await user.click(screen.getByTestId('otp-toggle'));
    expect(screen.queryByTestId('email-input')).toBeNull();
    await user.click(screen.getByTestId('link-toggle'));
    expect(screen.getByTestId('email-input')).not.toBeNull();
  });
});
