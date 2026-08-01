import { evidenceBody, evidenceExternalLink } from '../../lib/algernon/feedEvidence';

// Renders an item's evidence `body` as readable multiline PROSE (paragraph flow,
// not a key:value row) — the digest text a peer_digest card was otherwise missing,
// and the bounded email preview an email_tier card carries (#26). Generic: any kind
// whose evidence carries a `body` string gets this. Escaped React text children only
// (evidence is untrusted — email bodies are attacker-controlled text, so NEVER markup:
// the #22 stored-XSS pin earns its keep here). Bounded + scrollable so a 4000-char
// digest / long email can't blow out its container.
//
// The ONE href exception (#26, blessed): a SERVER-built, prefix-allowlisted external
// link (evidenceExternalLink → the email's "Open in Gmail" deep-link) renders as a
// plain external anchor — href VERBATIM, never client-reconstructed; target=_blank +
// rel=noopener noreferrer (safe in a standalone PWA: opens the system browser without
// window.opener access; the SW leaves cross-origin nav untouched). Anything not on the
// Gmail allowlist yields no anchor at all.
//
// Renders nothing when there's neither a body nor a link (today's items degrade cleanly).
export function EvidenceBody({ evidence }: { evidence: unknown }) {
  const body = evidenceBody(evidence);
  const link = evidenceExternalLink(evidence);
  if (!body && !link) return null;
  return (
    <div data-testid="evidence-body" className="mt-2">
      {body && (
        <p className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-honeydew-200 bg-honeydew-50 px-3 py-2 text-xs leading-relaxed text-honeydew-700">
          {body.text}
        </p>
      )}
      {body?.truncated && (
        <p data-testid="evidence-truncated" className="mt-1 text-[11px] italic text-honeydew-600/80">
          {link ? 'There’s more — open in Gmail.' : 'Truncated — full text in the Brief.'}
        </p>
      )}
      {link && (
        <a
          data-testid="evidence-external-link"
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-block text-xs font-semibold text-honeydew-700 underline underline-offset-2"
        >
          {link.label} ↗
        </a>
      )}
    </div>
  );
}
