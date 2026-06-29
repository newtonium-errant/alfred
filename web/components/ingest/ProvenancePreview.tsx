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
  className,
}: {
  target?: IngestTarget;
  recordType: string;
  title: string;
  source: string;
  ingestedBy: string;
  originInstance: string;
  className?: string;
}) {
  return (
    <div
      data-testid="ingest-provenance"
      className={cn(
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
    </div>
  );
}
