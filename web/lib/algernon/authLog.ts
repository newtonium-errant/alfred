// SERVER-ONLY. Outcome logging for the BFF's `/api/auth/*` routes.
//
// WHY THIS EXISTS: the OTP routes were log-silent on EVERY path, success
// included. During the 2026-07-27 and 2026-08-03 first-attempt sign-in failures
// the transport logged `web.auth.otp_verify_ok` both times while the BFF side of
// the same request was a total void — so there was no way to tell whether the
// BFF had returned `{ok:true}` (and the browser lost the response) or had
// collapsed the upstream 200 into its uniform 401. An auth route that says
// nothing on the happy path cannot be diagnosed when the happy path is the thing
// that broke; per the intentionally-left-blank principle it must report what it
// did on every path, and "it worked" is a path.
//
// REDACTION CONTRACT (security-load-bearing — this is the reason the logging
// lives in a helper instead of an inline template literal at each call site):
// a line may NEVER carry the passcode, the session token, or a full email
// address. The only identity fragment emitted is the email DOMAIN, which is
// enough to correlate against the transport's own `web.auth.otp_*` lines
// without writing an operator's address into the journal.

/** Coarse bucket for an upstream status — what a grep wants, without a cardinality explosion. */
export type StatusClass = '2xx' | '3xx' | '4xx' | '5xx' | 'none' | 'other';

export function statusClass(status: number | null | undefined): StatusClass {
  if (typeof status !== 'number' || !Number.isFinite(status)) return 'none';
  if (status >= 200 && status < 300) return '2xx';
  if (status >= 300 && status < 400) return '3xx';
  if (status >= 400 && status < 500) return '4xx';
  if (status >= 500 && status < 600) return '5xx';
  return 'other';
}

/**
 * The email DOMAIN only — never the local part. Returns `(none)` for anything
 * that isn't a plausible address (absent, non-string, no `@`, empty local part,
 * empty domain), so a malformed input can never fall through as a raw echo of
 * whatever the caller submitted.
 */
export function emailDomain(email: unknown): string {
  if (typeof email !== 'string') return '(none)';
  const at = email.lastIndexOf('@');
  if (at < 1 || at === email.length - 1) return '(none)';
  return email.slice(at + 1).trim().toLowerCase() || '(none)';
}

/** Longest any single field may render. A pathological upstream body must not flood the journal. */
const MAX_FIELD_CHARS = 120;

/**
 * Make a value safe to interpolate into a ONE-LINE log record.
 *
 * The `error` field carries a string relayed straight from the upstream body and
 * is otherwise unvalidated, so it is an injection vector into the journal. The
 * guarantee here is that a VALUE can never be mistaken for STRUCTURE:
 *  - control characters (a newline above all) would forge a whole extra line —
 *    `error=x\n[bff:auth/otp/verify] outcome=ok` greps as a genuine success;
 *  - `=` and `[` `]` would forge FIELDS or a record prefix INSIDE one line, which
 *    a newline guard alone does not stop: neutralising only the newline still
 *    leaves `error=rate_limited?[bff:auth/otp/verify]_outcome=ok` matching a
 *    naive `grep outcome=ok`. Both classes go.
 *  - spaces collapse to `_`, so a multi-word value cannot split into two fields.
 *
 * Sanitising rather than allowlisting the known codes is deliberate: an
 * allowlist would silently render any NEW backend error code as `unknown`,
 * blinding exactly the incident that introduced it. Legitimate codes are
 * snake_case identifiers, so none of the replaced characters costs real signal.
 */
export function sanitizeLogValue(value: string | number | boolean): string {
  return String(value)
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f\u007f-\u009f=[\]]/g, '?')
    .replace(/\s/g, '_')
    .slice(0, MAX_FIELD_CHARS);
}

export interface AuthOutcome {
  /** Terse, greppable verb for what the route did (`ok`, `rejected`, `sent`, …). */
  outcome: string;
  /** Upstream transport status, or null/undefined when no relay was attempted. */
  upstream?: number | null;
  /** Redacted to its domain before it is written. Never logged whole. */
  email?: unknown;
  /** Extra scalar fields. Callers must never put a code/token/address in here. */
  extra?: Record<string, string | number | boolean>;
  /** `warn` for anything an operator should notice; defaults to `log`. */
  level?: 'log' | 'warn';
}

/**
 * Emit one `[bff:<route>] outcome=… upstream=… status_class=… email_domain=…`
 * line, matching the existing BFF convention (feed/act, ingest/shortcut).
 * `upstream=none` when the route answered without relaying.
 */
export function logAuthOutcome(route: string, o: AuthOutcome): void {
  // EVERY interpolated field goes through the sanitiser, not just `extra.error`.
  // `email_domain` is caller-supplied upstream of emailDomain() and so can carry
  // control bytes too ("a@b\nc"), which makes it a second forging vector; `route`
  // and `outcome` are call-site literals today, and sanitising them costs nothing
  // and removes the question for whoever adds the next call site.
  const parts = [
    `outcome=${sanitizeLogValue(o.outcome)}`,
    `upstream=${typeof o.upstream === 'number' ? o.upstream : 'none'}`,
    `status_class=${statusClass(o.upstream)}`,
    `email_domain=${sanitizeLogValue(emailDomain(o.email))}`,
  ];
  for (const [k, v] of Object.entries(o.extra ?? {})) {
    parts.push(`${sanitizeLogValue(k)}=${sanitizeLogValue(v)}`);
  }
  const line = `[bff:${sanitizeLogValue(route)}] ${parts.join(' ')}`;
  if (o.level === 'warn') console.warn(line);
  else console.log(line);
}
