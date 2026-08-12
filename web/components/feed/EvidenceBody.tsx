import { FencedText } from '../markdown/FencedText';
import { evidenceBody, evidenceExternalLink, isEmailEvidence } from '../../lib/algernon/feedEvidence';
import { WARM_SURFACE, type KnownSurface, type SurfaceIdentity } from '../../lib/algernon/consoleTokens';

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
//
// SHARED BETWEEN TWO IDENTITIES. This renders inside the console deck card AND
// inside the warm feed row, so the palette is a prop rather than a rewrite.
// `warm` is the default and renders exactly the classes this component always
// rendered — a surface that has not adopted the console identity passes nothing
// and this component changes not at all.
//
// Scoped to THIS COMPONENT deliberately. It says nothing about the surrounding
// page: the shared Layout now labels every surface with a `data-surface`
// attribute, so a page's own root markup is not unchanged even where this
// component's output is. The narrower claim is the true one, and the broader
// one is the misreading worth foreclosing.
// The feed lane flips its own call site when it adopts.
//
// KEYED ON `KnownSurface`, NOT `SurfaceIdentity` — and that is load-bearing,
// not a tidy-up. `SurfaceIdentity` is an OPEN union (`KnownSurface | (string &
// {})`), and `Record<open-union, T>` degenerates to a string index signature:
// the lookup below would type as `T` rather than `T | undefined`, so TypeScript
// would report nothing at all for a name with no entry. Keying the table on the
// closed union is what keeps the compiler able to see the gap; the fallback in
// `skinFor` is what makes the gap survivable at runtime. Both halves, or
// neither works.
const SKIN: Record<KnownSurface, { frame: string; text: string; note: string; link: string }> = {
  warm: {
    frame: 'max-h-64 overflow-y-auto rounded-lg border border-honeydew-200 bg-honeydew-50 px-3 py-2',
    text: 'whitespace-pre-wrap break-words text-xs leading-relaxed text-honeydew-700',
    note: 'mt-1 text-[11px] italic text-honeydew-600/80',
    link: 'mt-2 inline-block text-xs font-semibold text-honeydew-700 underline underline-offset-2',
  },
  console: {
    frame: 'max-h-64 overflow-y-auto rounded-sm border border-console-edge bg-console-void px-3 py-2',
    text: 'whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-console-ink-dim',
    note: 'mt-1 text-[11px] italic text-console-ink-faint',
    link: 'mt-2 inline-block text-[11px] font-bold uppercase tracking-[0.14em] text-affirm underline underline-offset-4',
  },
};

/**
 * The skin for a surface name, falling back to `warm` for any name this
 * component has no entry for.
 *
 * The same contract as Layout's `surfaceClasses`, and it exists for the same
 * reason: `surface` accepts ANY name (a surface owns its skin in its own
 * stylesheet and adopts the prop without waiting on a change here), so a bare
 * lookup would throw on the first unregistered one. This component is rendered
 * inside surfaces it has never heard of by design — that is the point of the
 * seam — and a shared component that crashes its host is not a seam.
 */
function skinFor(name: SurfaceIdentity) {
  return (SKIN as Record<string, (typeof SKIN)[KnownSurface] | undefined>)[name] ?? SKIN[WARM_SURFACE];
}

export function EvidenceBody({
  evidence,
  surface = WARM_SURFACE,
}: {
  evidence: unknown;
  surface?: SurfaceIdentity;
}) {
  const skin = skinFor(surface);
  const body = evidenceBody(evidence);
  const link = evidenceExternalLink(evidence);
  if (!body && !link) return null;
  return (
    <div data-testid="evidence-body" className="mt-2">
      {body && (
        // #85: a fenced block in digest / email evidence gets a download
        // button. Escaping is unchanged — see FencedText's docstring.
        <div className={skin.frame}>
          <FencedText text={body.text} nameHint="evidence" className={skin.text} />
        </div>
      )}
      {body?.truncated && (
        <p data-testid="evidence-truncated" className={skin.note}>
          {link
            ? 'There’s more — open in Gmail.'
            : isEmailEvidence(evidence)
              ? 'Preview only — full text isn’t linkable here.'
              : 'Truncated — full text in the Brief.'}
        </p>
      )}
      {link && (
        <a
          data-testid="evidence-external-link"
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className={skin.link}
        >
          {link.label} ↗
        </a>
      )}
    </div>
  );
}
