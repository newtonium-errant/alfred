import { useEffect, useRef, useState } from 'react';
import { Button } from './ui/button';
import { Textarea } from './ui/textarea';
import {
  MAX_BUGREPORT_DESCRIPTION_CHARS,
  MAX_BUGREPORT_SCREENSHOT_BYTES,
  boundDescription,
  bugReportErrorMessage,
  buildBugReportContext,
  formatBytes,
  isAllowedShotType,
  type ShotMime,
} from '../lib/algernon/bugReport';
import { blobToBase64 } from '../lib/algernon/screenCapture';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';
const APP_VERSION = process.env.NEXT_PUBLIC_COMMIT_SHA || 'dev';

// How long the success confirmation stays up before the dialog closes itself.
const SUCCESS_DISMISS_MS = 2200;

/**
 * The media type to declare for `blob`, derived from the blob ITSELF.
 *
 * This must never be a constant. The box names the stored file
 * `<report-id>.<ext>` from whatever media type it is told, so declaring PNG for
 * JPEG bytes writes a file whose name disagrees with its content — exactly what
 * `routes_bugreport.py` refuses to do and what the BFF's zod pairing check
 * exists to prevent. Two server layers guard this; a hardcoded value here walks
 * around both of them, because it makes the lie internally consistent.
 *
 * PNG is the fallback rather than a refusal because the only blob that can
 * arrive with an absent or unrecognised type is the AUTO-CAPTURE, which is
 * always PNG (`captureScreen` encodes it, and `downscaleImage` re-encodes to
 * PNG). Picked files cannot reach here with a bad type — `handlePickFile`
 * refuses anything outside the allowlist before it is ever attached.
 */
function shotMediaType(blob: Blob | null): ShotMime {
  const type = blob?.type ?? '';
  return isAllowedShotType(type) ? (type as ShotMime) : 'image/png';
}

type ReportBugModalProps = {
  /** Route captured AT OPEN — frozen so a later navigation can't rewrite it. */
  route: string;
  /**
   * The auto-capture of the screen as it looked when the dialog OPENED, taken
   * before the dialog rendered. `null` when the capture failed or is still
   * running; the reporter can still send words, and can retry the capture.
   */
  initialShot: Blob | null;
  /** True while a capture/recapture is in flight. */
  capturing: boolean;
  /**
   * Re-run the capture of the underlying screen. Resolves to the new shot, or
   * `null` when it failed/timed out — the MODAL decides what to do with that,
   * and it KEEPS the prior shot rather than nuking it.
   */
  onRetake: () => Promise<Blob | null>;
  /** Close (Cancel, backdrop, Escape, or after a successful submit). */
  onClose: () => void;
};

/**
 * The in-app bug reporter (#95 v1).
 *
 * DETERMINISTIC CAPTURE ONLY. There is no AI intake conversation here — the
 * reporter types what happened, the app attaches what it already knows, and it
 * is filed. Honeydew's conversational intake is deferred; the seam it would
 * slot into is `description`, which an intake would rewrite before submit
 * without changing anything downstream.
 *
 * SEND-NOW-ALWAYS. `canSubmit` derives ONLY from
 * `(boundDescription(description) !== null && !submitting)` — never from
 * capture state. A reporter whose screenshot is still rendering, or failed
 * outright, can always still send their words. The screenshot is a
 * convenience; the words are the report.
 */
export function ReportBugModal({
  route,
  initialShot,
  capturing,
  onRetake,
  onClose,
}: ReportBugModalProps) {
  const [description, setDescription] = useState('');
  const [shot, setShot] = useState<Blob | null>(initialShot);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filedTo, setFiledTo] = useState<string | null>(null);
  // Soft note when a retake fails — we keep the prior shot rather than losing it.
  const [retakeNote, setRetakeNote] = useState<string | null>(null);

  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Cleared on unmount so it can never call onClose() (a parent setState) after
  // the dialog is gone.
  const successTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Adopt a capture that finishes AFTER mount (the button opens the dialog
  // immediately and the shot streams in) — but only while the reporter hasn't
  // already replaced or removed it themselves.
  const userTouchedShotRef = useRef(false);
  useEffect(() => {
    if (!userTouchedShotRef.current) setShot(initialShot);
  }, [initialShot]);

  // A live object URL for whatever blob is currently attached; revoked on
  // change and on unmount so blob URLs never leak.
  const previewUrlRef = useRef<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  useEffect(() => {
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    if (shot && typeof URL.createObjectURL === 'function') {
      const url = URL.createObjectURL(shot);
      previewUrlRef.current = url;
      setPreviewUrl(url);
    } else {
      setPreviewUrl(null);
    }
    return () => {
      if (previewUrlRef.current) {
        URL.revokeObjectURL(previewUrlRef.current);
        previewUrlRef.current = null;
      }
    };
  }, [shot]);

  useEffect(() => {
    bodyRef.current?.focus();
    return () => {
      if (successTimerRef.current) clearTimeout(successTimerRef.current);
    };
  }, []);

  // Escape closes (unless a submit is in flight) + focus trap.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !submitting) {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const root = dialogRef.current;
      if (!root) return;
      const focusables = Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !root.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, submitting]);

  function handlePickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    if (!isAllowedShotType(file.type)) {
      setError('That image type isn’t supported — PNG, JPEG or WebP only.');
      return;
    }
    if (file.size > MAX_BUGREPORT_SCREENSHOT_BYTES) {
      // The cap is named up front, in the units the reporter sees, alongside
      // what their file actually weighs — "too big" alone is not actionable.
      setError(
        `That image is ${formatBytes(file.size)} — the limit is ${formatBytes(MAX_BUGREPORT_SCREENSHOT_BYTES)}. Please attach a smaller one.`,
      );
      return;
    }
    setError(null);
    setRetakeNote(null);
    userTouchedShotRef.current = true;
    setShot(file);
  }

  function handleRemoveShot() {
    setRetakeNote(null);
    userTouchedShotRef.current = true;
    setShot(null);
  }

  async function handleRetake() {
    setError(null);
    setRetakeNote(null);
    userTouchedShotRef.current = true;
    const next = await onRetake();
    if (next) {
      setShot(next);
    } else {
      // KEEP the prior shot. A failed retake that discarded the working capture
      // would punish the reporter for trying to improve it.
      setRetakeNote(
        shot
          ? 'Couldn’t recapture — keeping the previous screenshot.'
          : 'Couldn’t capture this screen. You can still send the description.',
      );
    }
  }

  const bounded = boundDescription(description);
  const canSubmit = bounded !== null && !submitting;

  async function handleSubmit() {
    if (submitting) return;
    if (bounded === null) {
      // Refused honestly rather than sent as a blank report.
      setError('Please describe what went wrong before sending.');
      bodyRef.current?.focus();
      return;
    }
    setSubmitting(true);
    setError(null);

    let screenshotB64: string | null = null;
    if (shot) {
      screenshotB64 = await blobToBase64(shot, MAX_BUGREPORT_SCREENSHOT_BYTES);
      if (!screenshotB64) {
        // The words must not be lost to a picture problem — say what happened
        // and stop, rather than silently dropping the screenshot the reporter
        // can see attached, or silently dropping the whole report.
        setError(
          `That screenshot couldn’t be attached (the limit is ${formatBytes(MAX_BUGREPORT_SCREENSHOT_BYTES)}). Remove it and send just the description.`,
        );
        setSubmitting(false);
        return;
      }
    }

    try {
      const res = await fetch('/api/bugreport/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          description: bounded,
          context: buildBugReportContext({
            route,
            instance: INSTANCE_NAME,
            userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : '',
            viewportW: typeof window !== 'undefined' ? window.innerWidth : 0,
            viewportH: typeof window !== 'undefined' ? window.innerHeight : 0,
            appVersion: APP_VERSION,
          }),
          ...(screenshotB64
            ? { screenshot_b64: screenshotB64, screenshot_media_type: shotMediaType(shot) }
            : {}),
        }),
      });
      const payload = (await res.json().catch(() => null)) as
        | { error?: string; detail?: string; instance?: string }
        | null;
      if (!res.ok) {
        setError(bugReportErrorMessage(payload?.error ?? '', payload?.detail));
        setSubmitting(false);
        return;
      }
      // ILB: a successful submit says so VISIBLY, and names where it landed —
      // "sent" with no destination leaves the reporter unable to check.
      setFiledTo(payload?.instance || INSTANCE_NAME);
      setSubmitting(false);
      successTimerRef.current = setTimeout(() => onClose(), SUCCESS_DISMISS_MS);
    } catch {
      setError('We couldn’t reach the server. Your report was NOT saved — please try again.');
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-bug-title"
        data-testid="report-bug-modal"
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-y-auto rounded-t-2xl border border-honeydew-200 bg-white p-5 shadow-lg sm:rounded-2xl"
      >
        <div className="flex items-start justify-between gap-3">
          <h2 id="report-bug-title" className="text-lg font-bold text-honeydew-700">
            Report a bug <span aria-hidden="true">🐛</span>
          </h2>
          <button
            type="button"
            data-testid="report-bug-close"
            aria-label="Close"
            onClick={onClose}
            disabled={submitting}
            className="-mr-1 -mt-1 rounded-lg px-2 py-1 text-lg leading-none text-honeydew-600 hover:bg-honeydew-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 disabled:opacity-70"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        {filedTo ? (
          <p data-testid="report-bug-success" className="mt-5 text-honeydew-700">
            Report sent — filed to {filedTo}. ✓
          </p>
        ) : (
          <>
            <p className="mt-2 text-sm text-honeydew-600/80">
              Tell us what went wrong. We’ll attach the screen you’re on, which page
              you were on, and your app version.
            </p>

            <label
              htmlFor="report-bug-body"
              className="mt-4 text-sm font-semibold text-honeydew-700"
            >
              What happened?
            </label>
            <Textarea
              id="report-bug-body"
              ref={bodyRef}
              data-testid="report-bug-body"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={MAX_BUGREPORT_DESCRIPTION_CHARS}
              rows={4}
              placeholder="e.g. The Send button did nothing after I attached a photo."
              aria-label="Describe the bug"
              className="mt-1.5"
            />

            <div className="mt-4">
              <span className="text-sm font-semibold text-honeydew-700">Screenshot</span>
              {previewUrl ? (
                <div className="mt-1.5">
                  {/* eslint-disable-next-line @next/next/no-img-element -- local blob preview, not a remote asset */}
                  <img
                    src={previewUrl}
                    alt="Screenshot to attach"
                    data-testid="report-bug-preview"
                    className="max-h-48 w-full rounded-xl border border-honeydew-200 object-contain"
                  />
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      data-testid="report-bug-retake"
                      onClick={() => void handleRetake()}
                      disabled={submitting || capturing}
                    >
                      {capturing ? 'Capturing…' : 'Retake'}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      data-testid="report-bug-replace"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={submitting}
                    >
                      Replace
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid="report-bug-remove"
                      onClick={handleRemoveShot}
                      disabled={submitting}
                    >
                      Remove
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="mt-1.5">
                  <p data-testid="report-bug-no-shot" className="text-sm text-honeydew-600/80">
                    {capturing ? 'Capturing this screen…' : 'No screenshot attached.'}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      data-testid="report-bug-retake"
                      onClick={() => void handleRetake()}
                      disabled={submitting || capturing}
                    >
                      {capturing ? 'Capturing…' : 'Capture this screen'}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      data-testid="report-bug-attach"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={submitting}
                    >
                      Attach a file
                    </Button>
                  </div>
                </div>
              )}
              {retakeNote && (
                <p data-testid="report-bug-retake-note" className="mt-2 text-sm text-honeydew-600">
                  {retakeNote}
                </p>
              )}
              <p className="mt-2 text-xs text-honeydew-600/70">
                Up to {formatBytes(MAX_BUGREPORT_SCREENSHOT_BYTES)}, PNG/JPEG/WebP.
              </p>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                data-testid="report-bug-file"
                onChange={handlePickFile}
                className="hidden"
              />
            </div>

            {error && (
              <p
                data-testid="report-bug-error"
                role="alert"
                className="mt-3 text-sm text-red-600"
              >
                {error}
              </p>
            )}

            <div className="mt-5 flex flex-wrap items-center gap-3">
              <Button
                type="button"
                data-testid="report-bug-submit"
                onClick={() => void handleSubmit()}
                disabled={!canSubmit}
              >
                {submitting ? 'Sending…' : 'Send report'}
              </Button>
              <button
                type="button"
                data-testid="report-bug-cancel"
                onClick={onClose}
                disabled={submitting}
                className="text-sm font-medium text-honeydew-700 hover:underline disabled:opacity-70"
              >
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
