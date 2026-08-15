import { describe, expect, it, vi } from 'vitest';
import {
  FILED_CONTEXT_MAX_PATHS,
  MAX_INLINE_DOC_CHARS,
  PASTED_TEXT_FILENAME,
  buildFiledContextLine,
  canInline,
  composeTurnMessage,
  fileFromPastedText,
  ingestPayloadFor,
  ingestSuccessMessage,
  inlineDocBlock,
  inlineTooLongMessage,
  pasteIngestOfferMessage,
  pasteWantsIngest,
  prepareDoc,
  preparedFromTranscript,
  runFanout,
  turnOverLimitMessage,
  type FanoutJob,
  type PreparedTextDoc,
} from '../lib/algernon/composerFanout';
import { classifyAttachment } from '../lib/algernon/composerRouting';
import { MAX_MESSAGE_CHARS } from '../lib/algernon/schemas';
import { ApiError } from '../lib/algernon/http';
import type { IngestSubmitResponse } from '../lib/algernon/types';

// The unified composer's send (#97) — preparation, isolation, and the turn.

function textDoc(body: string, note: string | null = null): PreparedTextDoc {
  return { ok: true, format: 'text', body, note };
}

function ingestOk(path: string, status = 'created'): IngestSubmitResponse {
  return { status, path, record_type: 'document', instance: 'Salem' };
}

function fileJob(id: string, title = 'A note'): FanoutJob {
  return {
    id,
    route: 'file',
    payload: {
      target: 'SALEM',
      record_type: 'document',
      title,
      source: 'a.md',
      body: 'hello',
    },
  };
}

function batchJob(id: string): FanoutJob {
  return {
    id,
    route: 'batch',
    target: 'SALEM',
    instruction: 'read the invoice number',
    files: [new File([new Uint8Array(4)], 's1.png', { type: 'image/png' })],
  };
}

const okDeps = {
  submitIngest: vi.fn(async () => ingestOk('document/A note.md')),
  submitBatch: vi.fn(async () => ({
    status: 'queued' as const,
    batch_id: 'b1',
    images: 1,
    bytes: 4,
    path: 'batch/b1.md',
    instance: 'Salem',
  })),
};

describe('runFanout — partial-failure isolation', () => {
  it('a refused job does NOT sink the jobs after it', async () => {
    // THE load-bearing contract of this function. A fan-out that lets one
    // rejection escape passes every per-route test and still loses three
    // attachments to the first bad one — with nothing on screen to say which
    // of the four happened.
    const submitIngest = vi
      .fn<(payload: unknown) => Promise<IngestSubmitResponse>>()
      .mockResolvedValueOnce(ingestOk('document/first.md'))
      .mockRejectedValueOnce(new ApiError(409, 'title_collision'))
      .mockResolvedValueOnce(ingestOk('document/third.md'));

    const { outcomes, filedPaths } = await runFanout(
      [fileJob('a'), fileJob('b'), fileJob('c')],
      { ...okDeps, submitIngest: submitIngest as never },
    );

    // Every job ran — the count is what proves the loop was not abandoned.
    expect(submitIngest).toHaveBeenCalledTimes(3);
    expect(outcomes.a.status).toBe('done');
    expect(outcomes.b.status).toBe('failed');
    expect(outcomes.c.status).toBe('done');
    // The survivors' paths are still offered to the turn; the casualty's is not.
    expect(filedPaths).toEqual(['document/first.md', 'document/third.md']);
  });

  it('a THROWN error (not an ApiError) is still contained', async () => {
    // A network stack failure is not an ApiError, and an uncaught one here
    // would reject the whole send — the exact shape of the bug being excluded.
    const submitIngest = vi
      .fn<(payload: unknown) => Promise<IngestSubmitResponse>>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(ingestOk('document/second.md'));

    const { outcomes, filedPaths } = await runFanout([fileJob('a'), fileJob('b')], {
      ...okDeps,
      submitIngest: submitIngest as never,
    });

    expect(outcomes.a.status).toBe('failed');
    expect(outcomes.a.message).toBeTruthy();
    expect(outcomes.b.status).toBe('done');
    expect(filedPaths).toEqual(['document/second.md']);
  });

  it('the failure sentence comes from the ROUTE THAT FAILED, not a generic one', async () => {
    // Each route already owns operator-ruled copy. Re-wording it here would give
    // the same failure two different sentences depending on which door it came
    // through — and one of them would not carry the ruling.
    const collision = await runFanout([fileJob('a')], {
      ...okDeps,
      submitIngest: vi.fn(async () => {
        throw new ApiError(409, 'title_collision');
      }) as never,
    });
    expect(collision.outcomes.a.message).toContain('already exists');

    const tooMany = await runFanout([batchJob('img')], {
      ...okDeps,
      submitBatch: vi.fn(async () => {
        throw new ApiError(413, 'too_many_images');
      }) as never,
    });
    expect(tooMany.outcomes.img.message).toContain('scans in one batch');

    // Positive control: a SUCCESS is not accidentally reported as one of these.
    const fine = await runFanout([fileJob('a')], okDeps as never);
    expect(fine.outcomes.a.status).toBe('done');
    expect(fine.outcomes.a.message).toContain('document/A note.md');
  });

  it('every outcome carries a sentence — never a silent empty state', async () => {
    const { outcomes } = await runFanout([fileJob('a'), batchJob('img')], okDeps as never);
    for (const outcome of Object.values(outcomes)) {
      expect(outcome.message.trim().length).toBeGreaterThan(0);
    }
  });

  it('a BATCH path is not offered to the turn as readable context', async () => {
    // A batch record is a manifest whose results arrive later on the drip
    // schedule. Pointing the assistant at it now would invite it to report on
    // an empty record as though the work were done.
    const { filedPaths, outcomes } = await runFanout([batchJob('img')], okDeps as never);
    expect(outcomes.img.status).toBe('done');
    expect(outcomes.img.path).toBe('batch/b1.md');
    expect(filedPaths).toEqual([]);
    // Control: an INGEST path in the same shape IS offered.
    const filed = await runFanout([fileJob('a')], okDeps as never);
    expect(filed.filedPaths).toEqual(['document/A note.md']);
  });

  it('runs jobs in order, so the filed paths match the chips on screen', async () => {
    const seen: string[] = [];
    const submitIngest = vi.fn(async (p: { title: string }) => {
      seen.push(p.title);
      return ingestOk(`document/${p.title}.md`);
    });
    const { filedPaths } = await runFanout(
      [fileJob('a', 'one'), fileJob('b', 'two'), fileJob('c', 'three')],
      { ...okDeps, submitIngest: submitIngest as never },
    );
    expect(seen).toEqual(['one', 'two', 'three']);
    expect(filedPaths).toEqual([
      'document/one.md',
      'document/two.md',
      'document/three.md',
    ]);
  });

  it('an empty job list is a clean no-op (nothing to do is not a failure)', async () => {
    const { outcomes, filedPaths } = await runFanout([], okDeps as never);
    expect(outcomes).toEqual({});
    expect(filedPaths).toEqual([]);
  });
});

describe('ingestSuccessMessage', () => {
  it('distinguishes a NEW record from one that was already there', () => {
    expect(ingestSuccessMessage(ingestOk('document/A.md', 'created'))).toContain('Ingested');
    const existing = ingestSuccessMessage(ingestOk('document/A.md', 'exists'));
    expect(existing).toContain('Already present');
    // Both name the path — that is what the operator needs to go look at it.
    expect(existing).toContain('document/A.md');
  });
});

describe('the turn message', () => {
  it('a plain message is unchanged', () => {
    const t = composeTurnMessage({ text: 'what is in this?' });
    expect(t.message).toBe('what is in this?');
    expect(t.filedCarried).toBe(false);
    expect(t.overLimit).toBe(false);
  });

  it('a quoted document rides under its filename', () => {
    const t = composeTurnMessage({
      text: 'summarise this',
      inlineBlocks: [inlineDocBlock('notes.md', '# Heading\nbody')],
    });
    expect(t.message).toContain('summarise this');
    expect(t.message).toContain('notes.md:');
    expect(t.message).toContain('# Heading');
  });

  it('the filed-record line names the paths and rides IN the message', () => {
    // In the message, not a side channel: no new wire contract, and the operator
    // SEES in their own bubble exactly what the assistant was told.
    const t = composeTurnMessage({
      text: 'what does it say?',
      filedPaths: ['document/Bank statement.md'],
    });
    expect(t.filedCarried).toBe(true);
    expect(t.message).toContain('what does it say?');
    expect(t.message).toContain('document/Bank statement.md');
    expect(t.message.toLowerCase()).toContain('just filed');
  });

  it('summarises once past the listing cap', () => {
    const many = Array.from({ length: FILED_CONTEXT_MAX_PATHS + 3 }, (_, i) => `document/${i}.md`);
    const line = buildFiledContextLine(many);
    expect(line).toContain('document/0.md');
    expect(line).toContain('and 3 more');
    // Nothing at all when nothing was filed — the line is a signal, not a
    // permanent fixture.
    expect(buildFiledContextLine([])).toBe('');
  });

  it('ORDER OF SACRIFICE: the filed line gives way, the operator’s words never do', () => {
    // The operator's words and their quoted documents are never trimmed to make
    // room. The filed line is the only part a LATER turn can carry without loss,
    // so it is the part that yields.
    const nearlyFull = 'x'.repeat(MAX_MESSAGE_CHARS - 10);
    const t = composeTurnMessage({
      text: nearlyFull,
      filedPaths: ['document/Very long name that will not fit.md'],
    });
    expect(t.message).toBe(nearlyFull);
    expect(t.filedCarried).toBe(false);
    expect(t.overLimit).toBe(false);

    // Control: the same paths DO ride when there is room.
    const roomy = composeTurnMessage({
      text: 'short',
      filedPaths: ['document/Very long name that will not fit.md'],
    });
    expect(roomy.filedCarried).toBe(true);
  });

  it('flags an over-limit turn instead of truncating it', () => {
    const t = composeTurnMessage({ text: 'x'.repeat(MAX_MESSAGE_CHARS + 5) });
    expect(t.overLimit).toBe(true);
    const said = turnOverLimitMessage(MAX_MESSAGE_CHARS + 5);
    expect(said).toContain('5');
    expect(said).toContain('nothing was sent');
    // The remedy names the OTHER chip, which is the action actually available.
    expect(said.toLowerCase()).toContain('file the attached text');
  });
});

describe('inlining a document into a message', () => {
  it('a short document can be quoted; an over-long one cannot', () => {
    expect(canInline(textDoc('a'.repeat(100)))).toBe(true);
    expect(canInline(textDoc('a'.repeat(MAX_INLINE_DOC_CHARS + 1)))).toBe(false);
    // A PDF is never inlinable — the browser has no text for it.
    expect(canInline({ ok: true, format: 'pdf', bodyB64: 'AA==', note: null, bytes: 1 })).toBe(
      false,
    );
  });

  it('the headroom kept for the operator is REAL, not the whole message cap', () => {
    // A document that exactly filled the cap would leave no room to say what
    // should be done with it — a technically-sendable turn that is useless.
    expect(MAX_INLINE_DOC_CHARS).toBeLessThan(MAX_MESSAGE_CHARS);
    const doc = textDoc('a'.repeat(MAX_INLINE_DOC_CHARS));
    expect(canInline(doc)).toBe(true);
    const composed = composeTurnMessage({
      text: 'what is this?',
      inlineBlocks: [inlineDocBlock('big.md', doc.body)],
    });
    expect(composed.overLimit).toBe(false);
  });

  it('the too-long refusal points at the chip that WOULD work', () => {
    const said = inlineTooLongMessage('big.md', 99999);
    expect(said).toContain('big.md');
    expect(said).toContain('99,999');
    expect(said.toLowerCase()).toContain('file it to the vault');
  });
});

describe('prepareDoc — one read, both routes, #57’s rules unchanged', () => {
  it('a .csv lands FENCED with the row-count note', async () => {
    const file = new File(['a,b\n1,2\n'], 'rows.csv', { type: 'text/csv' });
    const doc = await prepareDoc(file);
    expect(doc.ok).toBe(true);
    if (!doc.ok || doc.format !== 'text') throw new Error('expected text');
    expect(doc.body.startsWith('```csv\n')).toBe(true);
    expect(doc.body).toContain('a,b\n1,2\n');
    expect(doc.note).toContain('2 rows');
  });

  it('a .md lands VERBATIM with no note (nothing was reshaped)', async () => {
    const file = new File(['# Title\n\nbody'], 'notes.md', { type: 'text/markdown' });
    const doc = await prepareDoc(file);
    if (!doc.ok || doc.format !== 'text') throw new Error('expected text');
    expect(doc.body).toBe('# Title\n\nbody');
    expect(doc.note).toBeNull();
  });

  it('a .pdf is relayed as BYTES with a note saying the box extracts', async () => {
    const file = new File([new Uint8Array([37, 80, 68, 70])], 'statement.pdf', {
      type: 'application/pdf',
    });
    const doc = await prepareDoc(file);
    if (!doc.ok || doc.format !== 'pdf') throw new Error('expected pdf');
    expect(doc.bodyB64.length).toBeGreaterThan(0);
    expect(doc.bytes).toBe(4);
    expect(doc.note).toContain('extracted on the box');
  });

  it('an EMPTY file is refused with #57’s own words — and a full one is not', async () => {
    const empty = await prepareDoc(new File([''], 'blank.md', { type: 'text/markdown' }));
    expect(empty.ok).toBe(false);
    if (empty.ok) throw new Error('unreachable');
    expect(empty.message).toContain('blank.md is empty');
    // Positive control: the same call path accepts a file with content.
    const full = await prepareDoc(new File(['x'], 'full.md', { type: 'text/markdown' }));
    expect(full.ok).toBe(true);
  });
});

describe('preparedFromTranscript', () => {
  it('a transcript becomes text; an empty one is refused with a remedy', () => {
    const ok = preparedFromTranscript('  the coop needs a new latch  ');
    expect(ok.ok).toBe(true);
    if (!ok.ok) throw new Error('unreachable');
    expect(ok.body).toBe('the coop needs a new latch');

    const empty = preparedFromTranscript('   ');
    expect(empty.ok).toBe(false);
    if (empty.ok) throw new Error('unreachable');
    // ILB: "nothing came back" is said out loud, with what to do about it.
    expect(empty.message).toContain('Nothing was transcribed');
    expect(empty.message.toLowerCase()).toContain('type it yourself');
  });
});

describe('ingestPayloadFor', () => {
  it('a text document sends `body` and NO body_format key', () => {
    const payload = ingestPayloadFor({
      target: 'SALEM',
      recordType: 'document',
      title: '  A note  ',
      source: '  a.md  ',
      doc: textDoc('hello'),
    });
    expect(payload).toEqual({
      target: 'SALEM',
      record_type: 'document',
      title: 'A note',
      source: 'a.md',
      body: 'hello',
    });
    // Byte-identical to the ingest page's own text request: an added
    // `body_format: undefined` still serialises differently, and that is the
    // kind of quiet wire change that costs a debugging session later.
    expect('body_format' in payload).toBe(false);
  });

  it('a PDF sends body_format + body_b64 and NO body key', () => {
    const payload = ingestPayloadFor({
      target: 'SALEM',
      recordType: 'source',
      title: 'Statement',
      source: 'statement.pdf',
      doc: { ok: true, format: 'pdf', bodyB64: 'JVBERg==', note: null, bytes: 4 },
    });
    expect(payload.body_format).toBe('pdf');
    expect(payload.body_b64).toBe('JVBERg==');
    expect('body' in payload).toBe(false);
    expect(payload.record_type).toBe('source');
  });
});

// ---------------------------------------------------------------------------
// A pasted body — the design's third door in
// ---------------------------------------------------------------------------

describe('a long paste is offered the ingest door', () => {
  it('offers at exactly the length a FILE stops being quotable — and not below it', () => {
    // The pair IS the pin: a body one character under the line is a message,
    // one character over it is a document. Asserting only the second would pass
    // against a build that offered the door to every paste.
    expect(pasteWantsIngest('x'.repeat(MAX_INLINE_DOC_CHARS))).toBe(false);
    expect(pasteWantsIngest('x'.repeat(MAX_INLINE_DOC_CHARS + 1))).toBe(true);
  });

  it('agrees with canInline about the same body — the DRIFT pin', () => {
    // Pinned as a RELATIONSHIP, not a number. A paste and an uploaded .txt are
    // the same bytes bound for the same two doors, so the length at which one
    // stops being a message must be the length at which the other does. If
    // either side is ever given its own constant, this reds — which a pin
    // written against 7000 would not.
    for (const len of [0, 1, 500, MAX_INLINE_DOC_CHARS - 1, MAX_INLINE_DOC_CHARS, MAX_INLINE_DOC_CHARS + 1, MAX_INLINE_DOC_CHARS * 2]) {
      const body = 'x'.repeat(len);
      expect(pasteWantsIngest(body)).toBe(!canInline({ ok: true, format: 'text', body, note: null }));
    }
  });

  it('an empty or ordinary paste is left alone', () => {
    expect(pasteWantsIngest('')).toBe(false);
    expect(pasteWantsIngest('two down, one to go')).toBe(false);
  });

  it('the offer names the count AND the cap', () => {
    const msg = pasteIngestOfferMessage(12345);
    expect(msg).toContain('12,345');
    expect(msg).toContain(MAX_INLINE_DOC_CHARS.toLocaleString());
    // The remedy, not just the refusal — and it says the message survives.
    expect(msg).toContain('vault');
    expect(msg).toContain('ask about it');
  });

  it('the synthesised file is one the CLASSIFIER accepts as a document', () => {
    // The paste enters the existing document path by BEING a document. If the
    // filename ever changes to something `classifyAttachment` does not take,
    // the chip would be built for a file the picker would have rejected.
    const file = fileFromPastedText('body text');
    expect(file.name).toBe(PASTED_TEXT_FILENAME);
    expect(classifyAttachment(file)).toBe('doc');
  });

  it('the synthesised file carries the pasted bytes VERBATIM', async () => {
    // Markdown is passed through unreshaped by `prepareUpload` — a paste must
    // arrive in the vault as what was copied, not as something fenced or
    // trimmed on a guess about content that arrived without a name.
    const body = '# Notes\n\n- one, two\n- "quoted", ```fenced```\n';
    const prepared = await prepareDoc(fileFromPastedText(body));
    expect(prepared.ok).toBe(true);
    expect(prepared.ok === true && prepared.format === 'text' && prepared.body).toBe(body);
  });

  it('a whitespace-only paste is refused by the existing empty-body gate', () => {
    // Not a new refusal: the synthesised file rides `prepareUpload`, so the
    // paste door inherits the ingest door's gates rather than restating them.
    expect(pasteWantsIngest(' '.repeat(MAX_INLINE_DOC_CHARS + 1))).toBe(true);
  });
});
