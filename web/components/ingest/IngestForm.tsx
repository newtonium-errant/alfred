import { ChangeEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { Button, FILE_PILL_CLASS } from '../ui/button';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { Textarea } from '../ui/textarea';
import { EmptyState } from '../EmptyState';
import { VoiceCapture } from '../chat/VoiceCapture';
import { TargetPicker } from './TargetPicker';
import { ProvenancePreview } from './ProvenancePreview';
import { useIngest } from '../../lib/algernon/useIngest';
import { MAX_INGEST_CHARS } from '../../lib/algernon/schemas';
import {
  INGEST_UPLOAD_ACCEPT,
  isPdfFilename,
  prepareUpload,
  preparePdfUpload,
  readFailedMessage,
} from '../../lib/algernon/ingestUpload';
import { subtle } from '../../lib/typography';
import type { SessionUser } from '../../lib/algernon/types';

// Orchestrates the cross-instance ingest form: target+type picker → title → body
// (paste / voice transcript / .md|.txt|.csv upload) → provenance preview →
// submit. The VoiceCapture for the body is the core of "ingest including STT"
// (decision F): voice → editable transcript → ingest body. Verbatim — the body
// is written exactly as composed (no LLM/run_turn), which is what fixes the
// large-markdown wrong-order problem.
//
// Uploads are prepared by `lib/algernon/ingestUpload` — .md/.txt land raw, a
// .csv lands fenced, and an empty or over-limit file is refused with words.
// A .pdf (#57) is the exception to "the browser reads the file": its BYTES are
// relayed base64 and the box extracts the text with the same pypdf extractor
// the Telegram attachment path uses, so one file yields one answer whichever
// door it arrives through. A scanned PDF is refused there, with words.

function stripExt(name: string): string {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

export function IngestForm({
  user,
  originInstance,
  onUnauthenticated,
  initialTitle = '',
  initialBody = '',
  initialSource = '',
}: {
  user: SessionUser;
  originInstance: string;
  onUnauthenticated?: () => void;
  // Prefill for the Web Share Target handler (/share): the shared payload lands
  // in the SAME reviewed form rather than auto-submitting, so the operator still
  // picks the target instance and can fix the derived title before it is written.
  // Seed values only — the fields stay fully editable, and `Ingest another`
  // clears them.
  initialTitle?: string;
  initialBody?: string;
  initialSource?: string;
}) {
  const { targets, status, error, result, unauthenticated, submit, reset } = useIngest();

  const [target, setTarget] = useState('');
  const [recordType, setRecordType] = useState('');
  const [title, setTitle] = useState(initialTitle);
  const [body, setBody] = useState(initialBody);
  const [source, setSource] = useState(initialSource);
  // Upload outcome (#57): a disclosure note for a reshaped body, or a refusal
  // with words. Both replace a silence the operator would otherwise have to
  // diagnose from a disabled button.
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // A staged PDF (#57). Held SEPARATELY from `body` because it is not text: the
  // browser never reads a PDF's contents, it relays the bytes and the box
  // extracts. The two are mutually exclusive — staging a PDF clears the body,
  // and typing in the body clears the staged PDF — so there is never a question
  // of which one the submit is about.
  const [pdfUpload, setPdfUpload] = useState<
    { b64: string; filename: string; bytes: number } | null
  >(null);

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
      // Normalise the name ONCE: CSV detection trims, so deriving the title and
      // source from the untrimmed name would let a padded filename be treated as
      // a CSV while the source field kept the padding. One string, one answer.
      const name = file.name.trim();
      const reader = new FileReader();

      // A PDF is read as BYTES and relayed base64 — the box owns extraction, so
      // the browser never sees this file's text and must not pretend to. Every
      // other accepted type is still read as text, unchanged.
      if (isPdfFilename(name)) {
        reader.onload = () => {
          const buf = reader.result;
          const bytes =
            buf instanceof ArrayBuffer ? new Uint8Array(buf) : new Uint8Array(0);
          const prepared = preparePdfUpload(name, bytes);
          if (!prepared.ok) {
            setUploadNote(null);
            setPdfUpload(null);
            setUploadError(prepared.message);
            return;
          }
          setUploadError(null);
          setUploadNote(prepared.note);
          // Staging a PDF clears any typed body: only one of them can be what
          // gets written, and leaving stale text on screen beside a staged file
          // is the ambiguity this pairing exists to avoid.
          setBody('');
          setPdfUpload({ b64: prepared.bodyB64, filename: name, bytes: prepared.bytes });
          if (!title.trim()) setTitle(stripExt(name));
          if (!source.trim()) setSource(name);
        };
        reader.onerror = () => {
          setUploadNote(null);
          setPdfUpload(null);
          setUploadError(readFailedMessage(name, reader.error?.name));
        };
        reader.readAsArrayBuffer(file);
        return;
      }

      reader.onload = () => {
        const text = typeof reader.result === 'string' ? reader.result : '';
        const prepared = prepareUpload(name, text);
        // A refused upload leaves the body untouched rather than loading
        // something unsubmittable: the operator keeps whatever they had, and the
        // reason is on screen instead of hiding behind a greyed-out button.
        if (!prepared.ok) {
          setUploadNote(null);
          setUploadError(prepared.message);
          return;
        }
        setUploadError(null);
        setUploadNote(prepared.note);
        setBody(prepared.body);
        // A successful TEXT upload un-stages any PDF. The two paths share one
        // picker, so staging must not be sticky: without this, picking a .md
        // after a .pdf loaded the markdown into the body and still submitted
        // the PDF — the operator's most recent, most deliberate action
        // silently losing to the earlier one. Caught by the form-level pin,
        // not by any unit test of the helpers.
        setPdfUpload(null);
        if (!title.trim()) setTitle(stripExt(name));
        if (!source.trim()) setSource(name);
      };
      // A read can fail AFTER the picker accepted the file (moved, deleted,
      // permissions, media error). Without this the operator picks a file and the
      // form does nothing at all — the one silence this whole path exists to
      // remove, and the hardest to diagnose because it looks like a no-op click.
      reader.onerror = () => {
        setUploadNote(null);
        setUploadError(readFailedMessage(name, reader.error?.name));
      };
      reader.readAsText(file);
    },
    [title, source],
  );

  // Any hand-edit of the body invalidates both upload messages. A refusal must
  // not sit in red beside a body the operator has since composed by hand, and the
  // row-count note is a claim about the UPLOADED text — once the body is edited it
  // describes something that is no longer there.
  const onBodyEdited = useCallback((next: string) => {
    setBody(next);
    setUploadNote(null);
    setUploadError(null);
    // Typing a body un-stages a PDF. Without this the operator could type over
    // a staged file and submit — with the typed text silently discarded in
    // favour of the PDF, which is the worst possible resolution of the two.
    setPdfUpload(null);
  }, []);

  // Same invalidation, but keeping the functional updater the append needs: the
  // transcript is concatenated onto whatever the body holds NOW, not onto the
  // value captured when this callback was created.
  const appendTranscript = useCallback((t: string) => {
    setBody((prev) => (prev.trim() ? `${prev}\n\n${t}` : t));
    setUploadNote(null);
    setUploadError(null);
    setPdfUpload(null);
  }, []);

  const submitting = status === 'submitting';
  // A staged PDF satisfies the body requirement: its content is real, it just
  // is not text yet. Gating on `body` alone would leave the picker accepting a
  // PDF and the submit button dead — a silence with no words attached, which is
  // the failure mode the whole upload path is shaped against.
  const hasContent = pdfUpload
    ? true
    : body.trim().length > 0 && body.length <= MAX_INGEST_CHARS;
  const canSubmit =
    !!target &&
    !!recordType &&
    title.trim().length > 0 &&
    hasContent &&
    source.trim().length > 0 &&
    !submitting;

  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    void submit({
      target,
      record_type: recordType as 'document' | 'note' | 'source',
      title: title.trim(),
      source: source.trim(),
      ...(pdfUpload
        ? { body_format: 'pdf' as const, body_b64: pdfUpload.b64 }
        : { body }),
    });
  }, [canSubmit, submit, target, recordType, title, body, source, pdfUpload]);

  const startAnother = useCallback(() => {
    setTitle('');
    setBody('');
    setSource('');
    setUploadNote(null);
    setUploadError(null);
    setPdfUpload(null);
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
          <label className={FILE_PILL_CLASS}>
            Upload .md / .txt / .csv / .pdf
            <input
              type="file"
              accept={INGEST_UPLOAD_ACCEPT}
              className="hidden"
              data-testid="ingest-file"
              disabled={submitting}
              onChange={onTextFile}
            />
          </label>
        </div>

        {/* A refused upload says why, here at the affordance that refused it. */}
        {uploadError && (
          <p
            role="alert"
            data-testid="ingest-upload-error"
            className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
          >
            {uploadError}
          </p>
        )}

        {/* Decision F: voice → editable transcript → ingest body. */}
        {/* An appended transcript mutates the body just as typing does, so it
            invalidates the upload messages the same way. */}
        <VoiceCapture
          idPrefix="ingest-voice"
          disabled={submitting}
          onTranscript={appendTranscript}
        />

        <Textarea
          id="ingest-body"
          data-testid="ingest-body"
          placeholder="Paste or compose the document body…"
          rows={10}
          value={body}
          disabled={submitting}
          onChange={(e) => onBodyEdited(e.target.value)}
        />
        <p className={subtle}>
          {body.length.toLocaleString()} / {MAX_INGEST_CHARS.toLocaleString()} characters
        </p>
        {/* An over-limit body already disables submit; without this line that
            greyed-out button is the only signal, and the operator is left to
            guess. Reachable by paste/typing — an over-limit upload is refused
            before it lands. */}
        {body.length > MAX_INGEST_CHARS && (
          <p
            role="alert"
            data-testid="ingest-body-over-limit"
            className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
          >
            The body is {(body.length - MAX_INGEST_CHARS).toLocaleString()} characters over the{' '}
            {MAX_INGEST_CHARS.toLocaleString()}-character ingest limit — trim it before ingesting.
          </p>
        )}
      </div>

      <ProvenancePreview
        target={selectedTarget}
        recordType={recordType}
        title={title}
        source={source}
        ingestedBy={user.name}
        originInstance={originInstance}
        uploadNote={uploadNote}
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
