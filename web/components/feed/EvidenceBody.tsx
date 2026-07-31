import { evidenceBody } from '../../lib/algernon/feedEvidence';

// Renders an item's evidence `body` as readable multiline PROSE (paragraph flow,
// not a key:value row) — the digest text a peer_digest card was otherwise missing.
// Generic: any kind whose evidence carries a `body` string gets this. Escaped
// React text children only (evidence is untrusted — never markup, never an href).
// Bounded + scrollable so a 4000-char digest can't blow out its container.
// Renders nothing when there's no body (today's items degrade cleanly).
export function EvidenceBody({ evidence }: { evidence: unknown }) {
  const body = evidenceBody(evidence);
  if (!body) return null;
  return (
    <div data-testid="evidence-body" className="mt-2">
      <p className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-honeydew-200 bg-honeydew-50 px-3 py-2 text-xs leading-relaxed text-honeydew-700">
        {body.text}
      </p>
      {body.truncated && (
        <p data-testid="evidence-truncated" className="mt-1 text-[11px] italic text-honeydew-600/80">
          Truncated — full text in the Brief.
        </p>
      )}
    </div>
  );
}
