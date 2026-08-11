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

/**
 * Map a relayed error to the sentence the operator reads.
 *
 * EXPORTED so the suite can pin the wording (#57 gate, WARN-1). It was
 * module-local, which meant the operator-ruled scanned-PDF copy — a plain
 * refusal that must NOT dangle vision or OCR as something coming later — was
 * guarded by a comment and nothing else. A ruling enforced by a comment is a
 * ruling that survives exactly until the next person edits the string.
 */
export function friendlyError(e: unknown): string {
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
      // #57 PDF refusals. Each names its OWN cause: six failures rendered as
      // one "something went wrong" is the operator-facing form of silence, and
      // none of these is fixable without knowing which one happened.
      case 'file_too_large':
        return 'That PDF is over the 10 MB limit. Split it and upload the parts separately.';
      case 'extracted_text_too_large':
        return "That PDF's text is too long to ingest. Split it and upload the parts separately.";
      case 'pdf_no_text_layer':
        // Operator-ruled 2026-08-07: a PLAIN refusal. Deliberately promises no
        // vision/OCR capability — that copy option was considered and NOT taken.
        return 'That PDF has no selectable text — it looks like a scan or a photo. Try a version saved as text, or paste the text in yourself.';
      case 'pdf_encrypted':
        return 'That PDF is password-protected, so its text could not be read. Save an unlocked copy and upload that.';
      case 'pdf_unreadable':
        return 'That file could not be read as a PDF — it may be damaged or only partly downloaded.';
      case 'pdf_support_unavailable':
        return "That instance can't read PDFs yet. Paste the text in instead.";
      case 'invalid_base64':
        return "That upload didn't arrive intact. Try selecting the file again.";
      case 'empty_file':
        return 'That file is empty — there is nothing to ingest.';
      case 'invalid_type':
        return "That record type isn't accepted by this target.";
      case 'empty_title':
      case 'empty_body':
        return 'A title and body are both required.';
      case 'invalid_request':
        return 'Please check the form and try again.';
      // #68 — three codes the transport really emits on this route that used to
      // land on the generic message. Each names its own cause, for the reason
      // the PDF block above states: a refusal the operator cannot act on is the
      // operator-facing form of silence.
      case 'invalid_json':
        // The BODY didn't parse at the instance. Nothing the operator typed
        // caused it — the form serialises its own payload — so the copy says
        // so rather than sending them to re-read a field. Distinct from
        // `invalid_base64`, which is a file that arrived damaged; this is a
        // request that arrived unreadable.
        return "That request didn't reach the instance in a form it could read. Try again — if it keeps happening that's a bug, not something you did.";
      case 'title_too_long':
        // Deliberately states NO number. The transport sends the real limit as
        // `max_chars`, but ApiError carries only status/code/detail, so a
        // figure here would be a second copy of a constant this layer cannot
        // read — wrong the day the box changes it, and wrong silently.
        return 'That title is too long — shorten it and try again.';
      case 'wrong_peer':
        // The security-relevant one, and the advice is the opposite of the
        // obvious one. The browser never supplies the peer token: `web_ingest`
        // is the BFF's own SERVER-side credential, so this refusal can only
        // mean that token is missing or wrong at the server. Telling the
        // operator to sign in again would send them through a logout that
        // cannot fix it — the same trap the brief routes avoid by mapping this
        // to a 502 rather than relaying a bare 401 (see bffError.ts).
        //
        // Says WHAT was refused and WHAT WON'T HELP, and names no token, peer
        // or check — an attacker learns nothing about which credential failed.
        return "That instance refused the request for identity reasons. That's a configuration problem on the server, not your sign-in — signing in again won't change it.";
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
