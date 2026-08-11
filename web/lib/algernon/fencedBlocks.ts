/**
 * Splitting daemon-authored text into plain segments and fenced code blocks (#85).
 *
 * The surfaces that carry this text — chat messages, the morning brief, ticket
 * bodies, feed evidence — have always rendered it as ESCAPED TEXT in a
 * `whitespace-pre-wrap` block, which means a fenced block currently shows its
 * literal backticks on screen and its contents are only extractable by
 * selecting and copying. This module is the read-side counterpart to
 * `ingestUpload.fenceCsv`, which is what PRODUCES those fences on the way in.
 *
 * Pure and DOM-free on purpose: the component renders what this returns, so
 * the parsing rules are testable without a render (the convention
 * `ingestUpload.ts` sets out).
 *
 * **Fences are not always three backticks.** `fenceCsv` opens with
 * `max(3, longest_backtick_run + 1)` so that content containing backticks can
 * still be fenced. A parser that matches exactly ``` therefore truncates
 * precisely the payloads that most need care. The rule implemented here is
 * CommonMark's: a closing fence must be at least as long as the opening one.
 */

/** A run of ordinary text between fences. */
export interface TextSegment {
  kind: 'text';
  text: string;
}

/** One fenced block. */
export interface FenceSegment {
  kind: 'fence';
  /** The raw info string, verbatim (`csv`, `json`, `` `` ``, …). */
  info: string;
  /** Lowercased first word of the info string — the language token. */
  lang: string;
  /** Block contents, without the fence lines. Retains internal newlines. */
  content: string;
}

export type Segment = TextSegment | FenceSegment;

const FENCE_OPEN = /^(\s{0,3})(`{3,}|~{3,})[ \t]*([^\n]*)$/;

/**
 * Split `text` into ordered text and fence segments.
 *
 * An UNCLOSED fence is deliberately returned as plain text rather than
 * swallowing the rest of the document into a code block: streamed assistant
 * replies arrive mid-fence, and a half-arrived block should keep reading as
 * text until its closing line lands, not flicker the tail of the message into
 * a panel and then back out.
 */
export function splitFencedBlocks(text: string): Segment[] {
  if (!text) return [];
  const lines = text.split('\n');
  const segments: Segment[] = [];
  let buffer: string[] = [];

  const flushText = () => {
    if (buffer.length === 0) return;
    const joined = buffer.join('\n');
    if (joined.length > 0) segments.push({ kind: 'text', text: joined });
    buffer = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const open = FENCE_OPEN.exec(lines[i]);
    // An info string is only valid on an opening fence; a line of bare
    // backticks that CLOSES nothing is just text.
    if (!open) {
      buffer.push(lines[i]);
      continue;
    }
    const marker = open[2];
    const info = open[3].trim();

    // Find the closing fence: same character, at least as long, nothing but
    // whitespace after it.
    const closeRe = new RegExp(`^\\s{0,3}${marker[0] === '`' ? '`' : '~'}{${marker.length},}\\s*$`);
    let close = -1;
    for (let j = i + 1; j < lines.length; j += 1) {
      if (closeRe.test(lines[j])) { close = j; break; }
    }
    if (close === -1) {
      // Unclosed — treat the opener as ordinary text and carry on.
      buffer.push(lines[i]);
      continue;
    }

    flushText();
    segments.push({
      kind: 'fence',
      info,
      lang: info.split(/\s+/)[0]?.toLowerCase() ?? '',
      content: lines.slice(i + 1, close).join('\n'),
    });
    i = close;
  }
  flushText();
  return segments;
}

/** Extensions for languages whose token isn't already the extension. */
const EXT_BY_LANG: Record<string, string> = {
  javascript: 'js',
  typescript: 'ts',
  markdown: 'md',
  python: 'py',
  yaml: 'yml',
  text: 'txt',
  '': 'txt',
};

/** The file extension a fence's contents should download with. */
export function extensionForLang(lang: string): string {
  const key = lang.toLowerCase();
  if (key in EXT_BY_LANG) return EXT_BY_LANG[key];
  // Only accept a token that is plausibly an extension — an info string can
  // carry arbitrary text ("csv title=Q3 results"), and that must not end up
  // in a filename.
  return /^[a-z0-9]{1,8}$/.test(key) ? key : 'txt';
}

/** The MIME type to hand the Blob. */
export function mimeForLang(lang: string): string {
  switch (lang.toLowerCase()) {
    case 'csv': return 'text/csv';
    case 'json': return 'application/json';
    case 'html': return 'text/html';
    default: return 'text/plain';
  }
}

/** Button label — per language, so a CSV says CSV and a fence with no info string still reads sensibly. */
export function downloadLabelForLang(lang: string): string {
  const key = lang.toLowerCase();
  if (!key) return 'Download';
  if (/^[a-z0-9]{1,8}$/.test(key)) return `Download as ${key.toUpperCase()}`;
  return 'Download';
}

/**
 * A safe download filename: `<base>.<ext>`.
 *
 * `base` comes from the caller's context (a record name, the brief's date) and
 * is sanitised rather than trusted — it reaches an `<a download>` attribute,
 * and path separators there are a directory-traversal hint no browser should
 * be handed. Everything outside a conservative allowlist collapses to `-`.
 */
export function downloadFilename(base: string, lang: string): string {
  const cleaned = (base || '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .slice(0, 60);
  return `${cleaned || 'download'}.${extensionForLang(lang)}`;
}
