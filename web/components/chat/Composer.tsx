import {
  ClipboardEvent,
  DragEvent,
  KeyboardEvent,
  useRef,
  useState,
} from 'react';
import { Textarea } from '../ui/textarea';
import { Button } from '../ui/button';
import { VoiceCapture } from './VoiceCapture';
import type { ChatKind } from '../../lib/algernon/types';
import {
  ALLOWED_IMAGE_MEDIA_TYPES,
  MAX_IMAGES_PER_TURN,
  type ImageAttachment,
} from '../../lib/algernon/schemas';
import { collectImageAttachments } from '../../lib/algernon/imageAttach';
import { useComposerText } from '../../lib/algernon/useComposerText';

// The message composer. Enter sends; Shift+Enter inserts a newline. An empty /
// whitespace-only message never sends UNLESS an image is attached. `disabled`
// covers the in-flight + booting states (the caller passes it) so a user can't
// double-send a turn. A confirmed voice transcript pre-fills the EDITABLE
// textarea (never auto-submits — the operator edits then presses Send); a
// transcript-seeded send is tagged kind:'voice' so the backend turn counter
// reflects it (decision H).
//
// Image-carry (parity #29): a hidden file input + paste/drop capture attach up
// to MAX_IMAGES_PER_TURN screenshots (mime-allowlisted, each ≤ MAX_IMAGE_BYTES
// decoded). Each is read to a bare base64 string (the `data:<mime>;base64,`
// prefix stripped) matching the backend wire shape. An IMAGE-ONLY send carries
// a placeholder caption so the backend's message_required gate is satisfied.
// The FE caps are UX-only; the backend re-validates as the fail-loud authority.
//
// Learned-vocabulary capture (#54): alongside the edited text, a voice-seeded
// send carries the RAW STT transcript so the backend can diff the two and learn
// what it mis-heard. See `useComposerText` for what exactly is carried and why.
//
// TWO SHARED HOMES, since the unified composer (#97) became a second door onto
// the same conversation route: the text/transcript/seed rules live in
// `useComposerText`, and the image caps + their refusal sentences live in
// `imageAttach.collectImageAttachments`. Both are behaviour this file used to
// own inline; both are now single-sourced so the two composers cannot drift on
// which images they accept or what a transcript carries.

const IMAGE_ONLY_PLACEHOLDER = '(image attached, no caption)';

export function Composer({
  onSend,
  disabled = false,
  seedText = null,
  onSeedConsumed,
}: {
  onSend: (
    text: string,
    kind?: ChatKind,
    images?: ImageAttachment[],
    transcript?: string,
  ) => void;
  disabled?: boolean;
  /**
   * Text handed back after a send failed against a dead session (#94c). The
   * operator wrote it; losing it to a deploy restart is what turned a
   * background annoyance into "my reply is gone" — he recovered a 50-minute
   * conversation's reply by screenshotting it into the next session.
   */
  seedText?: string | null;
  /** Called once the seed has been placed, so it is offered exactly once. */
  onSeedConsumed?: () => void;
}) {
  const text = useComposerText({ seedText, onSeedConsumed });
  const [images, setImages] = useState<ImageAttachment[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canSend = text.value.trim().length > 0 || images.length > 0;

  function submit() {
    if (disabled || !canSend) return;
    const trimmed = text.value.trim();
    const hasImages = images.length > 0;
    const caption = trimmed || IMAGE_ONLY_PLACEHOLDER;
    const transcript = text.takeTranscript();
    // Keep the 2-arg call for the text-only path (no behavioural drift for the
    // existing caller / tests); only widen when images or a transcript ride along.
    if (hasImages || transcript) {
      onSend(caption, text.kind, hasImages ? images : undefined, transcript);
    } else {
      onSend(caption, text.kind);
    }
    text.reset();
    setImages([]);
    setImageError(null);
  }

  // Ingest a batch of picked/pasted/dropped files, enforcing the UX caps and
  // surfacing ONE inline error (never a silent drop — intentionally-left-blank).
  //
  // The caps, the preparation and the refusal wording all live in
  // `collectImageAttachments`. Two things it does that are easy to get
  // backwards, and are explained where the code is: the downscale runs BEFORE
  // the size gate, and the DECODED byte length is the quantity being capped.
  //
  // Downscaling does NOT hold the wedge — the history trim does, by bounding
  // the image COUNT per request (see imageDownscale.ts). Downscaling is about
  // fidelity-per-byte, not about the dimension limit.
  async function addFiles(files: FileList | File[]) {
    const list = Array.from(files);
    if (list.length === 0) return;
    setImageError(null);
    const { accepted, error } = await collectImageAttachments(list, {
      alreadyCount: images.length,
    });
    if (accepted.length > 0) {
      setImages((prev) => [...prev, ...accepted].slice(0, MAX_IMAGES_PER_TURN));
    }
    if (error) setImageError(error);
  }

  function removeImage(index: number) {
    setImages((prev) => prev.filter((_, i) => i !== index));
    setImageError(null);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const pasted = Array.from(e.clipboardData?.files ?? []).filter((f) =>
      f.type.startsWith('image/'),
    );
    if (pasted.length > 0) {
      e.preventDefault();
      void addFiles(pasted);
    }
  }

  function onDrop(e: DragEvent<HTMLTextAreaElement>) {
    const dropped = Array.from(e.dataTransfer?.files ?? []).filter((f) =>
      f.type.startsWith('image/'),
    );
    if (dropped.length > 0) {
      e.preventDefault();
      void addFiles(dropped);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <VoiceCapture
        idPrefix="composer-voice"
        disabled={disabled}
        // #54 — the transcript lands in THIS input, editable in place, one Send.
        insertDirectly
        onTranscript={text.appendTranscript}
      />

      {images.length > 0 && (
        <ul
          data-testid="composer-image-row"
          className="flex flex-wrap gap-2"
          aria-label="Attached images"
        >
          {images.map((img, i) => (
            <li key={`${img.media_type}-${i}`} className="relative">
              <img
                data-testid="composer-image-preview"
                src={`data:${img.media_type};base64,${img.data}`}
                alt={`Attachment ${i + 1}`}
                className="h-16 w-16 rounded-lg object-cover"
              />
              <button
                type="button"
                data-testid="composer-image-remove"
                aria-label={`Remove attachment ${i + 1}`}
                disabled={disabled}
                onClick={() => removeImage(i)}
                className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-honeydew-700 text-xs text-white"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      {imageError && (
        <p
          data-testid="composer-image-error"
          role="alert"
          className="text-sm text-danger"
        >
          {imageError}
        </p>
      )}

      <form
        className="flex items-end gap-2"
        data-testid="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          ref={fileInputRef}
          data-testid="composer-file-input"
          type="file"
          accept={ALLOWED_IMAGE_MEDIA_TYPES.join(',')}
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) void addFiles(e.target.files);
            // Reset so re-picking the SAME file fires change again.
            e.target.value = '';
          }}
        />
        <Button
          type="button"
          variant="outline"
          data-testid="composer-attach"
          aria-label="Attach image"
          disabled={disabled || images.length >= MAX_IMAGES_PER_TURN}
          onClick={() => fileInputRef.current?.click()}
        >
          Attach
        </Button>
        <Textarea
          data-testid="composer-input"
          aria-label="Message"
          placeholder="Message…"
          rows={1}
          value={text.value}
          disabled={disabled}
          onChange={(e) => text.setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onDrop={onDrop}
          className="max-h-40 min-h-[44px]"
        />
        <Button
          type="submit"
          data-testid="composer-send"
          disabled={disabled || !canSend}
        >
          Send
        </Button>
      </form>
    </div>
  );
}
