import { describe, expect, it } from 'vitest';
import {
  INGEST_UPLOAD_ACCEPT,
  countCsvRows,
  csvUploadNote,
  fenceCsv,
  isCsvFilename,
  prepareUpload,
} from '../lib/algernon/ingestUpload';
import { MAX_INGEST_CHARS } from '../lib/algernon/schemas';

// #57 CSV half. The contract under test is LOSSLESSNESS plus honesty: a CSV body
// is fenced and otherwise untouched, .md/.txt stay byte-identical to what they
// were before this existed, and the two silent-absence cases (empty file,
// over-limit file) come back as words rather than a disabled button.

const CSV = 'name,qty\nwidget,3\nsprocket,12\n';

describe('isCsvFilename', () => {
  it('matches .csv case-insensitively', () => {
    expect(isCsvFilename('sales.csv')).toBe(true);
    expect(isCsvFilename('SALES.CSV')).toBe(true);
    expect(isCsvFilename('  sales.csv  ')).toBe(true);
  });

  it('does not match the text formats that already worked', () => {
    expect(isCsvFilename('notes.md')).toBe(false);
    expect(isCsvFilename('notes.txt')).toBe(false);
  });

  it('does not match a name that merely contains csv', () => {
    expect(isCsvFilename('csv-notes.md')).toBe(false);
    expect(isCsvFilename('export.csv.bak')).toBe(false);
  });
});

describe('countCsvRows — a newline count, header included', () => {
  it('counts every line including the header', () => {
    expect(countCsvRows(CSV)).toBe(3);
  });

  it('does not invent a trailing empty row for a newline-terminated file', () => {
    expect(countCsvRows('a,b\n1,2\n')).toBe(2);
    expect(countCsvRows('a,b\n1,2')).toBe(2);
  });

  it('counts CRLF and bare-CR line endings as one row each', () => {
    expect(countCsvRows('a,b\r\n1,2\r\n')).toBe(2);
    expect(countCsvRows('a,b\r1,2\r')).toBe(2);
  });

  it('is zero for an empty file', () => {
    expect(countCsvRows('')).toBe(0);
    expect(countCsvRows('\n')).toBe(0);
  });

  it('counts a quoted embedded newline as an extra LINE — which is why the note says so', () => {
    // Honest over-count: no CSV parse happens here, and the rendered note calls
    // itself a newline count for exactly this case.
    expect(countCsvRows('a,b\n"multi\nline",2\n')).toBe(3);
    expect(csvUploadNote('x.csv', 3)).toContain('newline count, header included');
  });
});

describe('fenceCsv — lossless', () => {
  it('wraps the content in a ```csv block without altering it', () => {
    const fenced = fenceCsv(CSV);
    expect(fenced).toBe('```csv\nname,qty\nwidget,3\nsprocket,12\n```\n');
  });

  it('recovers the ORIGINAL bytes when the fence is stripped back off', () => {
    const fenced = fenceCsv(CSV);
    const inner = fenced.slice(fenced.indexOf('\n') + 1, fenced.lastIndexOf('```\n'));
    expect(inner).toBe(CSV);
  });

  it('preserves CRLF line endings verbatim (only the row COUNT normalizes)', () => {
    expect(fenceCsv('a,b\r\n1,2\r\n')).toBe('```csv\na,b\r\n1,2\r\n```\n');
  });

  it('grows the fence past a backtick run in the data so the block cannot close early', () => {
    const withFence = 'name,snippet\nwidget,"```"\n';
    const fenced = fenceCsv(withFence);
    expect(fenced.startsWith('````csv\n')).toBe(true);
    expect(fenced.endsWith('\n````\n')).toBe(true);
    expect(fenced).toContain('widget,"```"');
  });

  it('grows the fence for a LONGER run too', () => {
    expect(fenceCsv('a,`````\n').startsWith('``````csv\n')).toBe(true);
  });

  it('adds the newline the closing fence needs when the file lacks one', () => {
    expect(fenceCsv('a,b\n1,2')).toBe('```csv\na,b\n1,2\n```\n');
  });
});

describe('prepareUpload — CSV', () => {
  it('fences a CSV body and notes the row count', () => {
    const r = prepareUpload('sales.csv', CSV);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body).toBe('```csv\nname,qty\nwidget,3\nsprocket,12\n```\n');
    expect(r.note).toBe(csvUploadNote('sales.csv', 3));
    expect(r.note).toContain('3 rows (newline count, header included)');
    expect(r.note).toContain('fenced verbatim in the body');
  });

  it('never parses or reshapes the cells — quotes, commas and blanks survive', () => {
    const gnarly = 'a,b\n"x,y",\n,"he said ""hi"""\n';
    const r = prepareUpload('odd.csv', gnarly);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body).toContain(gnarly);
  });
});

describe('prepareUpload — .md/.txt stay exactly as they were', () => {
  it('passes a .md body through raw, with no fence and no note', () => {
    const md = '# Title\n\nSome *markdown* body.\n';
    const r = prepareUpload('notes.md', md);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body).toBe(md);
    expect(r.note).toBeNull();
  });

  it('passes a .txt body through raw, preserving its own leading/trailing whitespace', () => {
    const txt = '  leading and trailing kept  \n\n';
    const r = prepareUpload('notes.txt', txt);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body).toBe(txt);
    expect(r.note).toBeNull();
  });
});

describe('prepareUpload — empty is said out loud, not submitted silently', () => {
  it('refuses a zero-byte file by name', () => {
    const r = prepareUpload('empty.csv', '');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.reason).toBe('empty');
    expect(r.message).toContain('empty.csv');
    expect(r.message).toContain('nothing to ingest');
  });

  it('refuses a whitespace-only file too (the box would 400 empty_body)', () => {
    const r = prepareUpload('blank.txt', '  \n\t\n');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.reason).toBe('empty');
  });

  it('does not fence an empty CSV into a non-empty body', () => {
    // The failure this pins: fencing first would produce '```csv\n\n```\n',
    // which passes the box's empty_body check and files a record of nothing.
    const r = prepareUpload('empty.csv', '');
    expect(r.ok).toBe(false);
  });
});

describe('prepareUpload — the size limit is named before the server ever sees it', () => {
  it('accepts a body exactly at the limit', () => {
    const r = prepareUpload('big.txt', 'x'.repeat(MAX_INGEST_CHARS));
    expect(r.ok).toBe(true);
  });

  it('refuses one character over, naming the file, BOTH numbers, and the no-op', () => {
    const over = MAX_INGEST_CHARS + 1;
    const r = prepareUpload('big.txt', 'x'.repeat(over));
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.reason).toBe('too_large');
    expect(r.message).toContain('big.txt');
    expect(r.message).toContain(over.toLocaleString());
    expect(r.message).toContain(MAX_INGEST_CHARS.toLocaleString());
    expect(r.message).toContain('Nothing was loaded');
    // A .txt body is not fenced, so the fence caveat must NOT appear.
    expect(r.message).not.toContain('once fenced');
  });

  it('measures the FENCED length for a CSV — the body the box will actually weigh', () => {
    // A CSV that fits by itself but not once fenced must be refused here, or the
    // operator meets the limit as a 413 relay instead of a sentence.
    const justUnder = `${'x'.repeat(MAX_INGEST_CHARS - 4)}\n`;
    expect(justUnder.length).toBeLessThan(MAX_INGEST_CHARS);
    const r = prepareUpload('tight.csv', justUnder);
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.reason).toBe('too_large');
    // The figure quoted is the fenced body's, which is what would be relayed.
    expect(r.message).toContain(fenceCsv(justUnder).length.toLocaleString());
    // …and it SAYS the fence is why the figure exceeds the file's own size, so
    // the sentence does not read as an arithmetic error to the operator.
    expect(r.message).toContain('once fenced');
    expect(r.message).not.toContain(justUnder.length.toLocaleString());
  });

  it('honours a caller-supplied limit (the box value is per-instance configurable)', () => {
    const r = prepareUpload('small.txt', 'abcdef', 5);
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.message).toContain('5-character');
  });

  it('errs EARLY on astral-plane characters, never toward a surprise 413', () => {
    // The box counts Python code points; this counts JS UTF-16 code units, so an
    // emoji is 2 here and 1 there (measured: '😀'.repeat(10) is length 20 in JS,
    // len 10 in Python). The consequence pinned here is the DIRECTION: a body the
    // box would accept can be refused locally, but never the reverse — so the
    // operator can never be bounced by a limit the form said they were under.
    const emoji = '😀'.repeat(4);
    expect(emoji.length).toBe(8); // UTF-16 units
    expect([...emoji].length).toBe(4); // code points, as the box would count
    const r = prepareUpload('faces.txt', emoji, 6);
    expect(r.ok).toBe(false); // refused at 8 > 6 even though the box would see 4
    if (r.ok) return;
    expect(r.message).toContain('8-character');
  });
});

describe('INGEST_UPLOAD_ACCEPT', () => {
  it('offers csv alongside the formats that already worked', () => {
    expect(INGEST_UPLOAD_ACCEPT).toContain('.csv');
    expect(INGEST_UPLOAD_ACCEPT).toContain('text/csv');
    expect(INGEST_UPLOAD_ACCEPT).toContain('.md');
    expect(INGEST_UPLOAD_ACCEPT).toContain('.txt');
    expect(INGEST_UPLOAD_ACCEPT).toContain('text/markdown');
    expect(INGEST_UPLOAD_ACCEPT).toContain('text/plain');
  });
});
