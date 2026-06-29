import { useEffect, useState } from 'react';
import { authApi } from './authClient';
import type { SessionUser } from './types';

// Resolves the current signed-in user (display only) from the BFF `/api/auth/me`,
// which reads the httpOnly identity cookie server-side. `user === null` after
// loading means "not signed in" → the chat page redirects to /login.

export interface UseSession {
  user: SessionUser | null;
  loading: boolean;
}

export function useSession(): UseSession {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    authApi
      .me()
      .then((u) => {
        if (active) setUser(u);
      })
      .catch(() => {
        // 401 (no/invalid session) or network error → treat as signed out.
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return { user, loading };
}
