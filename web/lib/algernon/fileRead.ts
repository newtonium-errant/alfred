/**
 * `FileReader` wrapped as promises — the one place the browser's read failures
 * become a rejected promise instead of a silent no-op (#97).
 *
 * Three doors now read picked files (the chat Composer's images, the ingest
 * body, the unified composer's attachments) and each had its own inline
 * `new FileReader()` with its own `onerror` handling — or, in one case, none at
 * all, which is the "picked a file and the form did nothing" silence
 * `ingestUpload.readFailedMessage` exists to describe. Wrapping it once means a
 * read failure is an exception every caller must decide about, rather than a
 * callback a caller can forget to write.
 *
 * The rejection carries the DOMException NAME where the browser supplies one,
 * because that is what `readFailedMessage` renders in its parenthetical and it
 * is the only diagnostic on offer for a file that vanished between the picker
 * and the read.
 */

/** The DOMException name, when the browser gave one — else undefined. */
export function readErrorName(e: unknown): string | undefined {
  if (e && typeof e === 'object' && 'name' in e) {
    const name = (e as { name?: unknown }).name;
    if (typeof name === 'string' && name) return name;
  }
  return undefined;
}

function readWith<T>(
  file: Blob,
  start: (reader: FileReader) => void,
  extract: (result: FileReader['result']) => T,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('read_failed'));
    reader.onload = () => {
      try {
        resolve(extract(reader.result));
      } catch (e) {
        reject(e);
      }
    };
    start(reader);
  });
}

/**
 * Read to a BARE standard-base64 string — the `data:<mime>;base64,` prefix
 * stripped, matching the backend's wire shape for a carried image.
 */
export function readAsBase64(file: Blob): Promise<string> {
  return readWith(
    file,
    (r) => r.readAsDataURL(file),
    (result) => {
      if (typeof result !== 'string') throw new Error('unexpected_reader_result');
      const comma = result.indexOf(',');
      return comma >= 0 ? result.slice(comma + 1) : result;
    },
  );
}

/** Read as text (the .md / .txt / .csv path). */
export function readAsText(file: Blob): Promise<string> {
  return readWith(
    file,
    (r) => r.readAsText(file),
    (result) => (typeof result === 'string' ? result : ''),
  );
}

/**
 * Read as raw bytes (the .pdf path — the browser never reads a PDF's text; it
 * relays the bytes and the box extracts, so one file yields one answer whichever
 * door it arrives through).
 */
export function readAsBytes(file: Blob): Promise<Uint8Array> {
  return readWith(
    file,
    (r) => r.readAsArrayBuffer(file),
    (result) => (result instanceof ArrayBuffer ? new Uint8Array(result) : new Uint8Array(0)),
  );
}
