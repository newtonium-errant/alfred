import { useCallback, useEffect, useState } from 'react';
import { ingestApi } from './client';
import { ApiError } from './http';
import type { IngestSubmitResponse, IngestTarget } from './types';
import type { IngestBody } from './schemas';

// Client-side state for the cross-instance ingest surface: loads the configured
// targets once, then drives a single verbatim submit. Mirrors useChat's shape
// (status machine + friendly errors + an `unauthenticated` flag the page uses to
// redirect to /login on a 401).

function isUnauthenticated(e: unknown): boolean {
  return e instanceof ApiError && e.status === 401 && e.code === 'invalid_session';
}

export type IngestStatus = 'loading' | 'ready' | 'submitting' | 'done' | 'error';

export interface UseIngest {
  targets: IngestTarget[];
  status: IngestStatus;
  error: string | null;
  result: IngestSubmitResponse | null;
  unauthenticated: boolean;
  submit: (payload: IngestBody) => Promise<void>;
  /** Clear a completed/failed submit so the form is reusable for the next artifact. */
  reset: () => void;
}

function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'invalid_session':
        return 'Your session has ended — please sign in again.';
      case 'forbidden':
        return 'Ingest is owner-only on this instance.';
      case 'unknown_target':
        return "That target instance isn't available. Pick another.";
      case 'title_collision':
        return 'A record with a very similar title already exists there — pick a different title.';
      case 'body_too_large':
        return 'That document is too large to ingest. Trim it and try again.';
      case 'invalid_type':
        return "That record type isn't accepted by this target.";
      case 'empty_title':
      case 'empty_body':
        return 'A title and body are both required.';
      case 'invalid_request':
        return 'Please check the form and try again.';
      case 'vault_not_configured':
        return "That instance isn't set up to receive documents yet.";
      case 'ingest_failed':
      case 'transport_unreachable':
      case 'network_error':
        return "Couldn't reach that instance right now. Try again shortly.";
      case 'transport_misconfigured':
        return 'Ingest isn’t configured on this instance yet.';
      default:
        return 'Something went wrong. Please try again.';
    }
  }
  return 'Something went wrong. Please try again.';
}

export function useIngest(enabled = true): UseIngest {
  const [targets, setTargets] = useState<IngestTarget[]>([]);
  const [status, setStatus] = useState<IngestStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IngestSubmitResponse | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);

  const fail = useCallback((e: unknown) => {
    if (isUnauthenticated(e)) setUnauthenticated(true);
    setStatus('error');
    setError(friendlyError(e));
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    setStatus('loading');
    ingestApi
      .targets()
      .then((r) => {
        if (!active) return;
        setTargets(r.targets);
        setStatus('ready');
      })
      .catch((e) => {
        if (active) fail(e);
      });
    return () => {
      active = false;
    };
  }, [enabled, fail]);

  const submit = useCallback(
    async (payload: IngestBody) => {
      setError(null);
      setResult(null);
      setStatus('submitting');
      try {
        const r = await ingestApi.submit(payload);
        setResult(r);
        setStatus('done');
      } catch (e) {
        fail(e);
      }
    },
    [fail],
  );

  const reset = useCallback(() => {
    setError(null);
    setResult(null);
    setStatus('ready');
  }, []);

  return { targets, status, error, result, unauthenticated, submit, reset };
}
