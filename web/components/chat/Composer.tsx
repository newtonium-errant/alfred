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
  MAX_IMAGE_BYTES,
  base64DecodedBytes,
  type ImageAttachment,
} from '../../lib/algernon/schemas';

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

const IMAGE_ONLY_PLACEHOLDER = '(image attached, no caption)';
const MAX_IMAGE_MIB = Math.round(MAX_IMAGE_BYTES / (1024 * 1024));

// Read a File to a bare standard-base64 string (data: prefix stripped).
function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('read_failed'));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        reject(new Error('unexpected_reader_result'));
        return;
      }
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

export function Composer({
  onSend,
  disabled = false,
}: {
  onSend: (text: string, kind?: ChatKind, images?: ImageAttachment[]) => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState('');
  // True once a voice transcript seeded the input — tags the next send as 'voice'.
  const [voiceSeeded, setVoiceSeeded] = useState(false);
  const [images, setImages] = useState<ImageAttachment[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canSend = value.trim().length > 0 || images.length > 0;

  function submit() {
    if (disabled || !canSend) return;
    const text = value.trim();
    const hasImages = images.length > 0;
    const caption = text || IMAGE_ONLY_PLACEHOLDER;
    // Keep the 2-arg call for the text-only path (no behavioural drift for the
    // existing caller / tests); only widen to 3 args when images ride along.
    if (hasImages) {
      onSend(caption, voiceSeeded ? 'voice' : 'text', images);
    } else {
      onSend(caption, voiceSeeded ? 'voice' : 'text');
    }
    setValue('');
    setVoiceSeeded(false);
    setImages([]);
    setImageError(null);
  }

  // Ingest a batch of picked/pasted/dropped files, enforcing the UX caps and
  // surfacing ONE inline error (never a silent drop — intentionally-left-blank).
  async function addFiles(files: FileList | File[]) {
    const list = Array.from(files);
    if (list.length === 0) return;
    setImageError(null);
    const accepted: ImageAttachment[] = [];
    let error: string | null = null;
    for (const file of list) {
      if (images.length + accepted.length >= MAX_IMAGES_PER_TURN) {
        error = `You can attach at most ${MAX_IMAGES_PER_TURN} images.`;
        break;
      }
      const mime = (file.type || '').toLowerCase();
      if (!(ALLOWED_IMAGE_MEDIA_TYPES as readonly string[]).includes(mime)) {
        error = 'Only PNG, JPEG, GIF, or WebP images are supported.';
        continue;
      }
      // `file.size` is the DECODED byte length — the same quantity the backend
      // caps — so this is the primary size gate; the base64 recheck below is
      // belt-and-suspenders against a reader that inflates.
      if (file.size > MAX_IMAGE_BYTES) {
        error = `Each image must be under ${MAX_IMAGE_MIB} MiB.`;
        continue;
      }
      try {
        const data = await readAsBase64(file);
        if (base64DecodedBytes(data) > MAX_IMAGE_BYTES) {
          error = `Each image must be under ${MAX_IMAGE_MIB} MiB.`;
          continue;
        }
        accepted.push({ media_type: mime as ImageAttachment['media_type'], data });
      } catch {
        error = 'Could not read that image.';
      }
    }
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
        onTranscript={(t) => {
          // APPEND, never replace. The old `setValue(t)` destroyed whatever the
          // operator had already typed — the clobber the operator hit. A single
          // space joins the two; no space when the input was empty, so a
          // voice-only message carries no leading whitespace.
          setValue((prev) => {
            const base = prev.trimEnd();
            return base ? `${base} ${t}` : t;
          });
          setVoiceSeeded(true);
        }}
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
          accept="image/png,image/jpeg,image/gif,image/webp"
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
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setValue(e.target.value);
            // Cleared back to empty → the next send is a plain text turn again.
            if (e.target.value.trim().length === 0) setVoiceSeeded(false);
          }}
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
