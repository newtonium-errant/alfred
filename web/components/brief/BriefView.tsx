import { FencedText } from '../markdown/FencedText';
import { subtle } from '../../lib/typography';

/**
 * Strip a leading YAML frontmatter block from a spooled artifact.
 *
 * The daemons spool their markdown with the record's frontmatter still attached,
 * and this view renders markdown as ESCAPED TEXT — so the operator was reading
 * `type: run`, `status: completed` and friends as the first lines of his brief.
 * Machine metadata rendered as prose.
 *
 * Deliberately narrow: only a block that OPENS ON THE FIRST LINE is removed, and
 * only up to its matching close. A `---` used mid-document as a horizontal rule
 * is untouched, and an unterminated opening fence returns the text UNCHANGED
 * rather than swallowing the document — the failure direction is showing too
 * much, never eating the brief.
 */
export function stripFrontmatter(markdown: string): string {
  const lines = markdown.split('\n');
  if (lines.length === 0 || lines[0].trim() !== '---') return markdown;
  for (let i = 1; i < lines.length; i += 1) {
    if (lines[i].trim() === '---') {
      return lines.slice(i + 1).join('\n').replace(/^\n+/, '');
    }
  }
  // No closing delimiter: not frontmatter, or a truncated artifact. Show it all.
  return markdown;
}

type BriefViewProps = {
  /** Section heading ("Morning Brief" / "Daily Sync"). */
  title: string;
  /** ISO date of the spooled artifact, or null when nothing spooled yet. */
  date: string | null;
  /** The raw markdown body, or null when nothing spooled yet. */
  markdown: string | null;
  /** ILB empty-state copy shown when date is null. */
  emptyMessage: string;
  testId: string;
};

// Renders one outbound artifact (morning brief / Daily Sync) as ESCAPED text —
// React text interpolation only, NEVER dangerouslySetInnerHTML (the markdown is
// daemon-authored but transits several renderers; escaping is non-negotiable).
// whitespace-pre-wrap + break-words keeps the markdown reading as-authored
// (headings, lists, line breaks) without any HTML rendering, and long unbroken
// tokens wrap instead of overflowing the column.
export function BriefView({ title, date, markdown, emptyMessage, testId }: BriefViewProps) {
  const empty = date === null || markdown === null;
  return (
    <section data-testid={testId}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-bold text-honeydew-700">{title}</h2>
        {!empty && (
          <span data-testid={`${testId}-date`} className={subtle}>
            {date}
          </span>
        )}
      </div>
      {empty ? (
        // Intentionally-left-blank: an explicit "nothing yet" line, never a
        // blank pane — "no artifact" must be distinguishable from "broken".
        <p data-testid={`${testId}-empty`} className={`mt-3 ${subtle}`}>
          {emptyMessage}
        </p>
      ) : (
        <div className="mt-3 rounded-xl border border-honeydew-200 bg-white px-4 py-3">
          {/* #85: fenced blocks (the ```csv the ingest path writes) get their
              own panel + a download button; everything else keeps the exact
              pre-wrap treatment it had. Still escaped text children only. */}
          <FencedText
            data-testid={`${testId}-content`}
            text={stripFrontmatter(markdown)}
            nameHint={testId}
            className="whitespace-pre-wrap break-words text-sm leading-relaxed text-neutral-800"
          />
        </div>
      )}
    </section>
  );
}
