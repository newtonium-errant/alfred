import { ChangeEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Button, FILE_PILL_CLASS } from '../ui/button';
import { Label } from '../ui/label';
import { Textarea } from '../ui/textarea';
import { EmptyState } from '../EmptyState';
import { BatchTargetPicker } from './BatchTargetPicker';
import type { BatchTarget, BatchTargetsResponse } from '../../lib/algernon/types';
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
  keyForStagedBatch,
  mib,
  prepareBatch,
  stagedBatchSignature,
  submitBatch,
  type BatchSubmitResponse,
  type StagedBatchKey,
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
  // #90 — which instance receives the batch. `null` while loading, so the
  // "nothing configured" empty state is not shown for the first render of a
  // deploy that is perfectly fine.
  const [targets, setTargets] = useState<BatchTarget[] | null>(null);
  const [target, setTarget] = useState('');
  // The idempotency key for what is currently staged (#100). Held in a REF, not
  // state: it must survive a failed attempt without re-rendering, and the whole
  // point is that pressing Submit a second time sends the SAME key. A change to
  // the files, the instruction or the target changes the signature and mints a
  // new one, so an edited submission is a new batch rather than a replay of the
  // old receipt.
  const idempotencyRef = useRef<StagedBatchKey | null>(null);

  useEffect(() => {
    let active = true;
    fetch('/api/batch/targets')
      .then((r) => (r.ok ? r.json() : { targets: [] }))
      .then((body: BatchTargetsResponse) => {
        if (!active) return;
        const list = body?.targets ?? [];
        setTargets(list);
        // Default to home, falling back to the first configured instance —
        // never to a hardcoded name.
        setTarget((list.find((t) => t.home) || list[0])?.name ?? '');
      })
      .catch(() => {
        if (active) setTargets([]);
      });
    return () => {
      active = false;
    };
  }, []);

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
    // Same staged set ⇒ same key, so a retry after a lost response is answered
    // with the original receipt instead of minting a second batch.
    const staged = keyForStagedBatch(
      idempotencyRef.current,
      stagedBatchSignature({ target, instruction: instruction.trim(), files }),
    );
    idempotencyRef.current = staged;
    try {
      // `submitBatch` owns the wire call for both batch surfaces — the query
      // -string target, the absent Content-Type and the header-borne key are
      // its three load-bearing details, stated once there rather than here.
      const body = await submitBatch(
        target,
        buildBatchForm(instruction.trim(), files),
        staged.key,
      );
      setResult(body);
      // RETIRE the key on success. Without this, an operator who deliberately
      // re-picks the same scans with the same instruction would rebuild the
      // same signature, resend the same key, and be handed the first batch's
      // receipt — a second batch they asked for and did not get. A key answers
      // for one submission; once that submission is answered, it is spent.
      idempotencyRef.current = null;
      setFiles([]);
      setTotalBytes(0);
      setInstruction('');
    } catch (e) {
      // A relayed refusal keeps its OWN code so it keeps its own sentence and
      // its own remedy; anything else (the fetch itself failed) is the network
      // case, which reads differently on purpose — "not submitted, try again"
      // rather than the generic apology.
      if (e instanceof ApiError && e.status === 401) onUnauthenticated?.();
      setSubmitError(
        friendlyBatchError(e instanceof ApiError ? e : new ApiError(0, 'network_error')),
      );
    } finally {
      setSubmitting(false);
    }
  }, [canSubmit, instruction, files, onUnauthenticated, target]);

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

  // Intentionally-left-blank: nothing wired for bulk upload is a real,
  // explicit state — not an inert picker over an empty list, and not a form
  // whose submit would 503. `null` means still loading, so a healthy deploy
  // never flashes this on first render.
  if (targets !== null && targets.length === 0) {
    return (
      <EmptyState
        icon="📷"
        title="No batch targets configured"
        message="No instance on this deployment is wired for bulk scan upload yet. Set the per-target batch env on the server to enable it."
        testId="batch-no-targets"
      />
    );
  }

  const activeTarget = targets?.find((t) => t.name === target);

  return (
    <form
      data-testid="batch-form"
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        void handleSubmit();
      }}
    >
      <BatchTargetPicker
        targets={targets ?? []}
        target={target}
        onTargetChange={setTarget}
        disabled={submitting}
      />

      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Label htmlFor="batch-files">Scans</Label>
          <label className={FILE_PILL_CLASS}>
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
          {activeTarget ? <>Sending to <strong>{activeTarget.label}</strong>. </> : null}
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
