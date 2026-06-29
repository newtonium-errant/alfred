// Validate a post-auth redirect target to prevent an open redirect. Ported
// verbatim from honeydew (web/pages/signin.tsx) — security-load-bearing.
//
// ONLY a same-origin relative path is allowed: must start with a single '/' and
// must NOT be protocol-relative ('//host') or contain a scheme/backslash.
// Anything else (absolute URL, '//evil.com', 'https://evil.com', backslashes,
// control/whitespace chars) falls back to '/'.
export function safeNextPath(raw: unknown): string {
  if (typeof raw !== 'string' || raw.length === 0) return '/';
  // Must be rooted at a SINGLE '/'.
  if (raw[0] !== '/') return '/';
  // Reject protocol-relative '//host' and backslash tricks '/\host' (browsers
  // may normalize '\' to '/').
  if (raw[1] === '/' || raw[1] === '\\') return '/';
  // Reject any backslash anywhere, plus any ASCII control char or whitespace
  // (incl. tab/newline/space), which browsers can strip/normalize in ways that
  // change the effective origin. Codepoint check avoids tricky regex ranges.
  for (const ch of raw) {
    const c = ch.codePointAt(0) ?? 0;
    if (ch === '\\' || c <= 0x20) return '/';
  }
  return raw;
}
