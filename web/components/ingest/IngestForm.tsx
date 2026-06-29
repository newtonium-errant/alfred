import { ChangeEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { Textarea } from '../ui/textarea';
import { EmptyState } from '../EmptyState';
import { VoiceCapture } from '../chat/VoiceCapture';
import { TargetPicker } from './TargetPicker';
import { ProvenancePreview } from './ProvenancePreview';
import { useIngest } from '../../lib/algernon/useIngest';
import { MAX_INGEST_CHARS } from '../../lib/algernon/schemas';
import { subtle } from '../../lib/typography';
import type { SessionUser } from '../../lib/algernon/types';

// Orchestrates the cross-instance ingest form: target+type picker → title → body
// (paste / voice transcript / .md|.txt upload) → provenance preview → submit. The
// VoiceCapture for the body is the core of "ingest including STT" (decision F):
// voice → editable transcript → ingest body. Verbatim — the body is written
// exactly as composed (no LLM/run_turn), which is what fixes the large-markdown
// wrong-order problem.

function stripExt(name: string): string {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

export function IngestForm({
  user,
  originInstance,
  onUnauthenticated,
}: {
  user: SessionUser;
  originInstance: string;
  onUnauthenticated?: () => void;
}) {
  const { targets, status, error, result, unauthenticated, submit, reset } = useIngest();

  const [target, setTarget] = useState('');
  const [recordType, setRecordType] = useState('');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [source, setSource] = useState('');

  // Default the picker to the first configured target once targets load.
  useEffect(() => {
    if (targets.length > 0 && !target) {
      setTarget(targets[0].name);
      setRecordType(targets[0].recordTypes[0] ?? '');
    }
  }, [targets, target]);

  // Bubble a 401 up so the page can redirect to /login (parity with chat).
  useEffect(() => {
    if (unauthenticated) onUnauthenticated?.();
  }, [unauthenticated, onUnauthenticated]);

  const selectedTarget = useMemo(
    () => targets.find((t) => t.name === target),
    [targets, target],
  );

  const onTargetChange = useCallback(
    (name: string) => {
      setTarget(name);
      const t = targets.find((x) => x.name === name);
      // Keep the chosen type if the new target still offers it, else reset.
      if (t && !t.recordTypes.includes(recordType)) {
        setRecordType(t.recordTypes[0] ?? '');
      }
    },
    [targets, recordType],
  );

  const onTextFile = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = ''; // allow re-selecting the same file
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const text = typeof reader.result === 'string' ? reader.result : '';
        setBody(text);
        if (!title.trim()) setTitle(stripExt(file.name));
        if (!source.trim()) setSource(file.name);
      };
      reader.readAsText(file);
    },
    [title, source],
  );

  const submitting = status === 'submitting';
  const canSubmit =
    !!target &&
    !!recordType &&
    title.trim().length > 0 &&
    body.trim().length > 0 &&
    body.length <= MAX_INGEST_CHARS &&
    source.trim().length > 0 &&
    !submitting;

  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    void submit({
      target,
      record_type: recordType as 'document' | 'note' | 'source',
      title: title.trim(),
      body,
      source: source.trim(),
    });
  }, [canSubmit, submit, target, recordType, title, body, source]);

  const startAnother = useCallback(() => {
    setTitle('');
    setBody('');
    setSource('');
    reset();
  }, [reset]);

  // Intentionally-left-blank: an explicit loading line, never a blank pane.
  if (status === 'loading') {
    return (
      <p data-testid="ingest-loading" className={subtle}>
        Loading ingest targets…
      </p>
    );
  }

  // Intentionally-left-blank: no targets configured is a real, explicit state.
  if (targets.length === 0) {
    return (
      <EmptyState
        icon="📥"
        title="No ingest targets configured"
        message="This deployment has no instances wired for document ingest yet. Set the per-target ingest env on the server to enable it."
        testId="ingest-no-targets"
      />
    );
  }

  // Success surface (decision: explicit created status, never silent).
  if (status === 'done' && result) {
    return (
      <div data-testid="ingest-success" className="flex flex-col gap-3">
        <p className="rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800">
          {result.status === 'exists' ? 'Already present' : 'Ingested'} to{' '}
          <span className="font-semibold">{result.instance}</span> as{' '}
          <span className="font-semibold">{result.record_type}</span> —{' '}
          <span className="break-words font-mono text-xs">{result.path}</span>
        </p>
        <div>
          <Button type="button" data-testid="ingest-another" onClick={startAnother}>
            Ingest another
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      data-testid="ingest-form"
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
    >
      <TargetPicker
        targets={targets}
        target={target}
        recordType={recordType}
        onTargetChange={onTargetChange}
        onRecordTypeChange={setRecordType}
        disabled={submitting}
      />

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ingest-title">Title</Label>
        <Input
          id="ingest-title"
          data-testid="ingest-title"
          placeholder="A clear, unique title"
          value={title}
          maxLength={300}
          disabled={submitting}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ingest-source">Source</Label>
        <Input
          id="ingest-source"
          data-testid="ingest-source"
          placeholder="Where did this come from? (URL, filename, note…)"
          value={source}
          maxLength={500}
          disabled={submitting}
          onChange={(e) => setSource(e.target.value)}
        />
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Label htmlFor="ingest-body">Body (written verbatim)</Label>
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50">
            Upload .md / .txt
            <input
              type="file"
              accept=".md,.txt,text/markdown,text/plain"
              className="hidden"
              data-testid="ingest-file"
              disabled={submitting}
              onChange={onTextFile}
            />
          </label>
        </div>

        {/* Decision F: voice → editable transcript → ingest body. */}
        <VoiceCapture
          idPrefix="ingest-voice"
          disabled={submitting}
          onTranscript={(t) => setBody((prev) => (prev.trim() ? `${prev}\n\n${t}` : t))}
        />

        <Textarea
          id="ingest-body"
          data-testid="ingest-body"
          placeholder="Paste or compose the document body…"
          rows={10}
          value={body}
          disabled={submitting}
          onChange={(e) => setBody(e.target.value)}
        />
        <p className={subtle}>
          {body.length.toLocaleString()} / {MAX_INGEST_CHARS.toLocaleString()} characters
        </p>
      </div>

      <ProvenancePreview
        target={selectedTarget}
        recordType={recordType}
        title={title}
        source={source}
        ingestedBy={user.name}
        originInstance={originInstance}
      />

      {error && (
        <p
          role="alert"
          data-testid="ingest-error"
          className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
        >
          {error}
        </p>
      )}

      <div>
        <Button type="submit" data-testid="ingest-submit" disabled={!canSubmit}>
          {submitting ? 'Ingesting…' : 'Ingest document'}
        </Button>
      </div>
    </form>
  );
}
