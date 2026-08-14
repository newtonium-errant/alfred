import { cn } from '../../lib/utils';
import type { IngestTarget } from '../../lib/algernon/types';

// Shows the EXACT provenance frontmatter that will be written, so the operator
// sees the auto-stamped metadata before committing a verbatim ingest. Display
// only — the actual values are assembled server-side in the BFF (ingested_at is
// stamped at submit; this preview labels it "now"). No secrets (target URL/token
// never appear).
function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex gap-2 text-sm">
      <span className="w-32 shrink-0 font-semibold text-honeydew-700">{k}</span>
      <span className="min-w-0 break-words text-honeydew-900">{v}</span>
    </div>
  );
}

export function ProvenancePreview({
  target,
  recordType,
  title,
  source,
  ingestedBy,
  originInstance,
  uploadNote,
  className,
}: {
  target?: IngestTarget;
  recordType: string;
  title: string;
  source: string;
  ingestedBy: string;
  originInstance: string;
  /**
   * An upload fact about the BODY (#57) — e.g. a CSV's row count and the fact
   * that it was fenced. Rendered below a divider, deliberately outside the
   * frontmatter rows: those rows promise to be the exact metadata written, and
   * folding a non-frontmatter figure in among them would make that claim false.
   */
  uploadNote?: string | null;
  className?: string;
}) {
  return (
    <div
      data-testid="ingest-provenance"
      className={cn(
        // REGISTER SEAM. This panel is a STRADDLER: /ingest is crt-registered and
        // /share is warm (it takes no surface prop and renders IngestForm), so the
        // warm classes below stay as the unmarked default and `ui-panel` is what
        // lets a register reach in. Converting these to console tokens instead
        // would have fixed /ingest and put a dark panel on a warm page.
        'ui-panel',
        'rounded-xl border border-honeydew-300 bg-honeydew-100 px-3 py-3',
        className,
      )}
    >
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-honeydew-600">
        Will be written as
      </p>
      <div className="flex flex-col gap-1">
        <Row k="target" v={target ? target.label : '—'} />
        <Row k="type" v={recordType || '—'} />
        <Row k="title" v={title.trim() || '—'} />
        <Row k="source" v={source.trim() || '—'} />
        <Row k="ingested_by" v={ingestedBy || '—'} />
        <Row k="ingested_at" v="now (stamped on submit)" />
        <Row k="ingested_via" v="web" />
        <Row k="origin_instance" v={originInstance} />
      </div>
      {uploadNote && (
        <p
          data-testid="ingest-upload-note"
          className="mt-3 border-t border-honeydew-300 pt-2 text-xs text-honeydew-700"
        >
          {uploadNote}
        </p>
      )}
    </div>
  );
}
