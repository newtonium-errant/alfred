import { subtle } from '../../lib/typography';

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
        <div
          data-testid={`${testId}-content`}
          className="mt-3 whitespace-pre-wrap break-words rounded-xl border border-honeydew-200 bg-white px-4 py-3 text-sm leading-relaxed text-neutral-800"
        >
          {markdown}
        </div>
      )}
    </section>
  );
}
