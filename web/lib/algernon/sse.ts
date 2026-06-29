// A minimal Server-Sent-Events frame parser for the chat stream consumer. SSE
// frames are separated by a blank line ("\n\n"); within a frame, `event:` names
// the type and `data:` carries the payload (multiple data lines join with "\n").
// Comment frames (lines starting with ":", e.g. the backend's `: keepalive`) carry
// no event and are skipped — they exist only to keep the wire warm (CONTRACT §1).
//
// `createSseParser().push(chunk)` is incremental: feed it decoded text chunks (a
// frame may split across chunks, or several frames may arrive in one chunk) and it
// returns the COMPLETE frames decoded so far, buffering any trailing partial.

export interface SseEvent {
  /** The SSE `event:` field; defaults to "message" when absent. */
  event: string;
  /** The joined `data:` payload (still a string — parse JSON at the call site). */
  data: string;
}

function parseFrame(raw: string): SseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of raw.split('\n')) {
    // Blank line or comment (`:` prefix, incl. `: keepalive`) → ignore.
    if (line === '' || line.startsWith(':')) continue;
    const idx = line.indexOf(':');
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? '' : line.slice(idx + 1);
    // A single leading space after the colon is part of the SSE format, not data.
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }
  // A frame with no data line (a pure comment/keepalive block) is not an event.
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n') };
}

export function createSseParser() {
  let buffer = '';
  return {
    /** Feed a decoded text chunk; returns the complete frames it completed. */
    push(chunk: string): SseEvent[] {
      buffer += chunk;
      const events: SseEvent[] = [];
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseFrame(raw);
        if (ev) events.push(ev);
      }
      return events;
    },
  };
}
