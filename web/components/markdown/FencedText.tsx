import { useCallback } from 'react';

import {
  type FenceSegment,
  downloadFilename,
  downloadLabelForLang,
  mimeForLang,
  splitFencedBlocks,
} from '../../lib/algernon/fencedBlocks';
import { Button } from '../ui/button';
import { cn } from '../../lib/utils';

/**
 * Renders daemon-authored text, giving fenced code blocks their own panel and a
 * download button (#85).
 *
 * ESCAPING IS UNCHANGED AND NON-NEGOTIABLE. Everything below renders as React
 * text children — no `dangerouslySetInnerHTML`, nowhere, ever. This is a
 * COMPONENT-TREE renderer, not a markdown-to-HTML one: it decides *which
 * element* a run of text goes into, never what markup that text becomes. The
 * #22 stored-XSS pin (a ticket body carrying `<script>` must render as visible
 * literal text) holds exactly as before, and its tests are the proof.
 *
 * Scope, deliberately: this is NOT a markdown renderer. Headings, lists, bold
 * and links continue to render as their literal source, because that is what
 * every one of these surfaces did yesterday and widening it would change four
 * views at once. The only thing that changes is that a fenced block becomes
 * extractable rather than a wall of backticks you have to select by hand.
 *
 * With no fences present the output is a single pre-wrap block — the same
 * markup as before, so unfenced content is untouched.
 */

/** Client-side only: no route, no upload, no new dependency. */
function downloadText(filename: string, mime: string, content: string): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  // Firefox requires the anchor to be in the document for a synthetic click to
  // fire; append and remove rather than assume Chrome's laxer behaviour.
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick, not synchronously: revoking before the browser has
  // started reading the URL cancels the download in Safari.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function Fence({
  segment,
  nameHint,
  bodyClassName,
}: {
  segment: FenceSegment;
  nameHint: string;
  bodyClassName: string;
}) {
  const { content, lang } = segment;
  const onDownload = useCallback(() => {
    downloadText(downloadFilename(nameHint, lang), mimeForLang(lang), content);
  }, [content, lang, nameHint]);

  return (
    <div data-testid="fenced-block" className="my-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span
          data-testid="fenced-block-lang"
          className="text-[11px] font-semibold uppercase tracking-wide text-honeydew-600"
        >
          {lang || 'text'}
        </span>
        <Button
          variant="outline"
          size="sm"
          type="button"
          data-testid="fenced-block-download"
          onClick={onDownload}
        >
          {downloadLabelForLang(lang)}
        </Button>
      </div>
      <pre
        data-testid="fenced-block-content"
        className={cn(
          'max-h-64 overflow-auto whitespace-pre rounded-lg border border-honeydew-300',
          'bg-honeydew-100 px-3 py-2 font-mono text-xs leading-relaxed text-honeydew-800',
          bodyClassName,
        )}
      >
        {content}
      </pre>
    </div>
  );
}

export function FencedText({
  text,
  nameHint = 'download',
  className,
  fenceBodyClassName = '',
  'data-testid': testId,
}: {
  text: string;
  /** Base for the download filename — a record name, a brief date. Sanitised. */
  nameHint?: string;
  /** Classes for the surrounding text blocks, so each surface keeps its own type scale. */
  className?: string;
  fenceBodyClassName?: string;
  'data-testid'?: string;
}) {
  const segments = splitFencedBlocks(text);
  const hasFence = segments.some((s) => s.kind === 'fence');

  // No fence → emit exactly the single pre-wrap block these surfaces already
  // had. Byte-identical markup for the overwhelmingly common case means this
  // change cannot regress ordinary messages.
  if (!hasFence) {
    return (
      <p data-testid={testId} className={className}>
        {text}
      </p>
    );
  }

  return (
    <div data-testid={testId}>
      {segments.map((segment, i) =>
        segment.kind === 'fence' ? (
          <Fence
            key={i}
            segment={segment}
            nameHint={nameHint}
            bodyClassName={fenceBodyClassName}
          />
        ) : (
          <p key={i} className={className}>
            {segment.text}
          </p>
        ),
      )}
    </div>
  );
}
