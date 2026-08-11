import { ChangeEvent, useCallback, useState } from 'react';
import { Button } from '../ui/button';
import { Label } from '../ui/label';
import { Textarea } from '../ui/textarea';
import { prepareImageForUpload } from '../../lib/algernon/imagePrepare';
import {
  ALLOWED_BATCH_MEDIA_TYPES,
  BATCH_UPLOAD_ACCEPT,
  MAX_BATCH_IMAGES,
  MAX_BATCH_IMAGE_BYTES,
  MAX_BATCH_INSTRUCTION_CHARS,
  MAX_BATCH_TOTAL_BYTES,
  batchSuccessMessage,
  buildBatchForm,
  friendlyBatchError,
  mib,
  prepareBatch,
  type BatchSubmitResponse,
} from '../../lib/algernon/batchSubmit';
import { ApiError } from '../../lib/algernon/http';
import { subtle } from '../../lib/typography';

// Bulk scan upload (#83): many images, ONE instruction that applies to all of
// them. Submitting SAVES the scans and mints the record that will carry the
// results; the extraction happens afterwards, one scan at a time, on the drip
// campaign's schedule. There is deliberately no live progress UI — progress
// lands in the record and in the brief, which is where the operator already
// looks, and a spinner promising otherwise would be a lie about a job that
// outlives the page.
//
// The caps are stated UP FRONT rather than only on refusal: an operator about
// to scan thirty pages should learn the limit before doing the work, not after.
// And each cap keeps its own refusal sentence with its own remedy — "send
// fewer", "send a smaller one" and "split the batch" are three different
// actions (see `prepareBatch`).

function fmtMiB(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1);
}

export function BatchForm({
  onUnauthenticated,
}: {
  onUnauthenticated?: () => void;
}) {
  const [instruction, setInstruction] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [totalBytes, setTotalBytes] = useState(0);
  const [pickError, setPickError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<BatchSubmitResponse | null>(null);

  const onPick = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const picked = Array.from(e.target.files ?? []);
      e.target.value = ''; // allow re-picking the same files
      if (picked.length === 0) return;
      setPickError(null);
      // Picking is additive, so the caps are evaluated against what is ALREADY
      // staged. Judging each pick in isolation would let three picks of 40
      // scans each sail past a 60-scan cap and be refused by the box after the
      // upload — the exact "honest at the edge" failure this avoids.
      const prepared = await prepareBatch(picked, prepareImageForUpload, {
        alreadyPicked: files.length,
        alreadyBytes: totalBytes,
      });
      if (prepared.files.length > 0) {
        setFiles((prev) => [...prev, ...prepared.files]);
        setTotalBytes(prepared.totalBytes);
      }
      setPickError(prepared.error);
    },
    [files.length, totalBytes],
  );

  const removeAt = useCallback((index: number) => {
    setFiles((prev) => {
      const next = prev.filter((_, i) => i !== index);
      setTotalBytes(next.reduce((sum, f) => sum + f.size, 0));
      return next;
    });
    setPickError(null);
  }, []);

  const clearAll = useCallback(() => {
    setFiles([]);
    setTotalBytes(0);
    setPickError(null);
  }, []);

  const instructionTooLong = instruction.length > MAX_BATCH_INSTRUCTION_CHARS;
  const canSubmit =
    files.length > 0 &&
    instruction.trim().length > 0 &&
    !instructionTooLong &&
    !submitting;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await fetch('/api/batch/submit', {
        method: 'POST',
        // NO Content-Type header: the browser must set it so the multipart
        // boundary is generated. Setting it by hand produces a body the box
        // cannot parse — a boundary-less multipart is unreadable, and the
        // failure looks like an empty batch rather than a malformed request.
        body: buildBatchForm(instruction.trim(), files),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = new ApiError(res.status, body?.error ?? 'network_error');
        if (res.status === 401) onUnauthenticated?.();
        setSubmitError(friendlyBatchError(err));
        return;
      }
      setResult(body as BatchSubmitResponse);
      setFiles([]);
      setTotalBytes(0);
      setInstruction('');
    } catch {
      setSubmitError(friendlyBatchError(new ApiError(0, 'network_error')));
    } finally {
      setSubmitting(false);
    }
  }, [canSubmit, instruction, files, onUnauthenticated]);

  const startAnother = useCallback(() => {
    setResult(null);
    setSubmitError(null);
    setPickError(null);
  }, []);

  // Success surface — explicit, and honest about whether anything will process
  // it (see `batchSuccessMessage`: "saved" and "queued" are different facts).
  if (result) {
    return (
      <div data-testid="batch-success" className="flex flex-col gap-3">
        <p
          className={
            result.status === 'saved'
              ? 'rounded-xl bg-amber-100 px-3 py-2 text-sm text-amber-900'
              : 'rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800'
          }
        >
          {batchSuccessMessage(result)}
        </p>
        <p className={subtle}>
          Record: <span className="break-words font-mono text-xs">{result.path}</span>
        </p>
        <div>
          <Button type="button" data-testid="batch-another" onClick={startAnother}>
            Upload another batch
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      data-testid="batch-form"
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        void handleSubmit();
      }}
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Label htmlFor="batch-files">Scans</Label>
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50">
            Add images
            <input
              id="batch-files"
              type="file"
              multiple
              accept={BATCH_UPLOAD_ACCEPT}
              className="hidden"
              data-testid="batch-files"
              disabled={submitting}
              onChange={onPick}
            />
          </label>
        </div>

        {/* The caps, stated BEFORE the operator does the scanning work. */}
        <p className={subtle} data-testid="batch-limits">
          Up to {MAX_BATCH_IMAGES} scans per batch, {mib(MAX_BATCH_IMAGE_BYTES)} MiB
          each and {mib(MAX_BATCH_TOTAL_BYTES)} MiB in total.{' '}
          {ALLOWED_BATCH_MEDIA_TYPES.map((t) => t.replace('image/', '').toUpperCase()).join(', ')}.
          Large scans are resized automatically before upload.
        </p>

        {pickError && (
          <p
            role="alert"
            data-testid="batch-pick-error"
            className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
          >
            {pickError}
          </p>
        )}

        {/* Intentionally-left-blank: an empty selection says so on purpose. */}
        {files.length === 0 ? (
          <p className={subtle} data-testid="batch-empty">
            No scans selected yet.
          </p>
        ) : (
          <div className="flex flex-col gap-1.5">
            <p className={subtle} data-testid="batch-count">
              {files.length} of {MAX_BATCH_IMAGES} scans — {fmtMiB(totalBytes)} of{' '}
              {mib(MAX_BATCH_TOTAL_BYTES)} MiB
            </p>
            <ul className="flex flex-col gap-1" data-testid="batch-list">
              {files.map((f, i) => (
                <li
                  key={`${f.name}-${i}`}
                  className="flex items-center justify-between gap-2 rounded-lg bg-honeydew-50 px-2 py-1 text-sm"
                >
                  <span className="truncate font-mono text-xs">{f.name}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className={subtle}>{fmtMiB(f.size)} MiB</span>
                    <button
                      type="button"
                      data-testid={`batch-remove-${i}`}
                      className="text-danger hover:underline"
                      disabled={submitting}
                      onClick={() => removeAt(i)}
                    >
                      Remove
                    </button>
                  </span>
                </li>
              ))}
            </ul>
            <div>
              <button
                type="button"
                data-testid="batch-clear"
                className="text-sm text-danger hover:underline"
                disabled={submitting}
                onClick={clearAll}
              >
                Remove all
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="batch-instruction">Instruction (applies to every scan)</Label>
        <Textarea
          id="batch-instruction"
          data-testid="batch-instruction"
          placeholder="e.g. Read the invoice number, date and total from this page."
          rows={4}
          value={instruction}
          disabled={submitting}
          onChange={(e) => setInstruction(e.target.value)}
        />
        <p className={subtle}>
          {instruction.length.toLocaleString()} /{' '}
          {MAX_BATCH_INSTRUCTION_CHARS.toLocaleString()} characters — sent with every
          scan, so keep it short.
        </p>
        {instructionTooLong && (
          <p
            role="alert"
            data-testid="batch-instruction-over-limit"
            className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
          >
            The instruction is{' '}
            {(instruction.length - MAX_BATCH_INSTRUCTION_CHARS).toLocaleString()}{' '}
            characters over the limit — trim it before submitting.
          </p>
        )}
      </div>

      {submitError && (
        <p
          role="alert"
          data-testid="batch-error"
          className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
        >
          {submitError}
        </p>
      )}

      <div>
        <Button type="submit" data-testid="batch-submit" disabled={!canSubmit}>
          {submitting ? 'Uploading…' : `Submit ${files.length || ''} scans`.trim()}
        </Button>
      </div>
    </form>
  );
}
