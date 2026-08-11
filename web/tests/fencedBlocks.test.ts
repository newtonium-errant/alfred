/**
 * #85 — the fenced-block splitter.
 *
 * Pure-function pins. The component renders what this returns, so the parsing
 * rules are worth getting right here rather than through a render.
 */
import { describe, expect, it } from 'vitest';

import {
  downloadFilename,
  downloadLabelForLang,
  extensionForLang,
  mimeForLang,
  splitFencedBlocks,
} from '../lib/algernon/fencedBlocks';
import { fenceCsv } from '../lib/algernon/ingestUpload';

const CSV = 'name,qty\nwidget,3\nsprocket,12\n';

describe('splitFencedBlocks', () => {
  it('returns a single text segment when there is no fence', () => {
    // The overwhelmingly common case — and the one the component turns back
    // into the exact pre-wrap block these surfaces already had.
    expect(splitFencedBlocks('just a reply')).toEqual([
      { kind: 'text', text: 'just a reply' },
    ]);
  });

  it('returns nothing for empty input', () => {
    expect(splitFencedBlocks('')).toEqual([]);
  });

  it('splits text / fence / text and captures the info string', () => {
    const out = splitFencedBlocks('before\n```csv\na,b\n1,2\n```\nafter');
    expect(out).toEqual([
      { kind: 'text', text: 'before' },
      { kind: 'fence', info: 'csv', lang: 'csv', content: 'a,b\n1,2' },
      { kind: 'text', text: 'after' },
    ]);
  });

  it('handles a fence with no info string', () => {
    const out = splitFencedBlocks('```\nplain\n```');
    expect(out).toEqual([{ kind: 'fence', info: '', lang: '', content: 'plain' }]);
  });

  it('lowercases the language but keeps the info string verbatim', () => {
    // The info string can carry more than a language ("csv title=Q3"); the
    // language token is just its first word.
    const [seg] = splitFencedBlocks('```CSV title=Q3\nx\n```');
    expect(seg).toMatchObject({ info: 'CSV title=Q3', lang: 'csv' });
  });

  it('matches a LONGER fence than three backticks', () => {
    // The load-bearing case. fenceCsv opens with max(3, longest_run + 1), so
    // CSV content containing backticks is fenced with four or more. A parser
    // hard-coded to ``` would close early and truncate exactly the payloads
    // that most need care.
    const out = splitFencedBlocks('````csv\na,`b`\n````');
    expect(out).toEqual([
      { kind: 'fence', info: 'csv', lang: 'csv', content: 'a,`b`' },
    ]);
  });

  it('does not close a long fence on a shorter BARE fence line inside it', () => {
    // CommonMark's rule: the closing fence must be at least as long as the
    // opener. A bare ``` line inside a ```` block is content, not a
    // terminator — and it has to be BARE to test anything. An earlier version
    // of this pin used "keep ```" (backticks with text before them), which no
    // closing-fence regex would match anyway; a mutation hard-coding the close
    // to three backticks passed it. This fixture fails that mutation.
    const [seg] = splitFencedBlocks('````\nkeep\n```\nmore\n````');
    expect(seg).toMatchObject({ kind: 'fence', content: 'keep\n```\nmore' });
  });

  it('round-trips what fenceCsv produces, including backtick-bearing CSV', () => {
    // The producer/consumer pin: ingestUpload writes these fences on the way
    // in, this reads them on the way out. If the two ever disagree about
    // fence length, downloads silently truncate.
    const hostile = 'name,note\nwidget,"has ``` in it"\n';
    const [seg] = splitFencedBlocks(fenceCsv(hostile));
    expect(seg).toMatchObject({ kind: 'fence', lang: 'csv' });
    expect((seg as { content: string }).content).toBe(hostile.trimEnd());
  });

  it('treats an UNCLOSED fence as ordinary text', () => {
    // A streamed reply arrives mid-fence. Swallowing the tail into a code
    // panel would make the message flicker into a block and back out as the
    // closing line lands.
    const out = splitFencedBlocks('here it comes\n```csv\na,b');
    expect(out).toEqual([{ kind: 'text', text: 'here it comes\n```csv\na,b' }]);
  });

  it('handles two fences in one document', () => {
    const out = splitFencedBlocks('```csv\na\n```\nmid\n```json\n{}\n```');
    expect(out.map((s) => s.kind)).toEqual(['fence', 'text', 'fence']);
    expect(out[2]).toMatchObject({ lang: 'json', content: '{}' });
  });

  it('preserves blank lines inside a fence', () => {
    const [seg] = splitFencedBlocks('```\na\n\nb\n```');
    expect(seg).toMatchObject({ content: 'a\n\nb' });
  });

  it('supports tilde fences', () => {
    const [seg] = splitFencedBlocks('~~~csv\na,b\n~~~');
    expect(seg).toMatchObject({ kind: 'fence', lang: 'csv', content: 'a,b' });
  });

  it('does not close a tilde fence with backticks', () => {
    // Mismatched markers must not terminate — that would truncate content.
    expect(splitFencedBlocks('~~~\na\n```')).toEqual([
      { kind: 'text', text: '~~~\na\n```' },
    ]);
  });
});

describe('filenames and labels', () => {
  it('maps languages to sensible extensions', () => {
    expect(extensionForLang('csv')).toBe('csv');
    expect(extensionForLang('json')).toBe('json');
    expect(extensionForLang('typescript')).toBe('ts');
    expect(extensionForLang('')).toBe('txt');
  });

  it('refuses a junk language as an extension', () => {
    // An info string is free text; it must never reach a filename raw.
    expect(extensionForLang('not a real language')).toBe('txt');
    expect(extensionForLang('../../etc/passwd')).toBe('txt');
  });

  it('sanitises the filename base', () => {
    // The result lands in an `<a download>` attribute — path separators there
    // are a traversal hint no browser should be handed.
    expect(downloadFilename('Morning Brief', 'csv')).toBe('Morning-Brief.csv');
    expect(downloadFilename('../../etc/passwd', 'csv')).toBe('etc-passwd.csv');
    expect(downloadFilename('', 'csv')).toBe('download.csv');
    expect(downloadFilename('...', 'csv')).toBe('download.csv');
  });

  it('caps the filename length', () => {
    expect(downloadFilename('x'.repeat(500), 'csv').length).toBeLessThanOrEqual(64);
  });

  it('labels the button per language, generically', () => {
    // Ruled small version: generic for any fence language, labelled per one.
    expect(downloadLabelForLang('csv')).toBe('Download as CSV');
    expect(downloadLabelForLang('json')).toBe('Download as JSON');
    expect(downloadLabelForLang('')).toBe('Download');
    expect(downloadLabelForLang('not a language')).toBe('Download');
  });

  it('picks a MIME type per language', () => {
    expect(mimeForLang('csv')).toBe('text/csv');
    expect(mimeForLang('json')).toBe('application/json');
    expect(mimeForLang('sql')).toBe('text/plain');
  });
});
