import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../lib/algernon/http';
import {
  ALLOWED_BATCH_MEDIA_TYPES,
  MAX_BATCH_IMAGES,
  MAX_BATCH_IMAGE_BYTES,
  MAX_BATCH_INSTRUCTION_CHARS,
  MAX_BATCH_TOTAL_BYTES,
  batchSuccessMessage,
  buildBatchForm,
  friendlyBatchError,
  prepareBatch,
  submitBatch,
} from '../lib/algernon/batchSubmit';

// #83 item 6 — the client half of bulk scan upload.
//
// Two things are pinned here and both are about HONESTY AT THE EDGE. First, the
// three caps must produce three DIFFERENT refusals: "send fewer", "send a
// smaller one" and "split the batch" are three different actions, and one
// message for all three cannot be acted on. Second, the front-end caps must
// equal the box's — a lower one silently forbids what the system allows, a
// higher one produces an upload refused after the fact.

function file(name: string, size: number, type = 'image/jpeg'): File {
  return new File([new Uint8Array(size)], name, { type });
}

/** Stand-in for the real canvas-based prepare (jsdom has no canvas). */
const passthrough = async (f: File, maxBytes: number) => ({
  file: f,
  withinBudget: f.size <= maxBytes,
});

describe('prepareBatch caps', () => {
  it('accepts a normal pick', async () => {
    const r = await prepareBatch(
      [file('a.jpg', 100), file('b.png', 200, 'image/png')],
      passthrough,
    );
    expect(r.files.map((f) => f.name)).toEqual(['a.jpg', 'b.png']);
    expect(r.totalBytes).toBe(300);
    expect(r.error).toBeNull();
  });

  it('refuses past the COUNT cap and names the file that hit it', async () => {
    const r = await prepareBatch(
      [file('a.jpg', 10), file('b.jpg', 10), file('over.jpg', 10)],
      passthrough,
      { maxImages: 2 },
    );
    expect(r.files).toHaveLength(2);
    expect(r.error).toContain('more than 2 scans');
    expect(r.error).toContain('over.jpg');
    // The remedy for THIS cap, and not the remedy for either other one.
    expect(r.error).toContain('start another batch');
    expect(r.error).not.toContain('resized');
  });

  it('refuses a single oversize image with the per-image remedy', async () => {
    const r = await prepareBatch(
      [file('ok.jpg', 10), file('huge.jpg', 5000)],
      passthrough,
      { maxImageBytes: 1000 },
    );
    expect(r.files.map((f) => f.name)).toEqual(['ok.jpg']);
    expect(r.error).toContain('huge.jpg');
    expect(r.error).toContain('lower-resolution scan');
    // NOT the batch-level remedy — a smaller batch would not fix one heavy scan.
    expect(r.error).not.toContain('start another batch');
  });

  it('refuses past the TOTAL cap while every image is individually fine', async () => {
    // Each image is comfortably under the per-image cap, so only the TOTAL cap
    // can be the one that fires — a build enforcing only the per-image budget
    // would accept this.
    const r = await prepareBatch(
      [file('a.jpg', 400), file('b.jpg', 400), file('c.jpg', 400)],
      passthrough,
      { maxImageBytes: 1000, maxTotalBytes: 900 },
    );
    expect(r.files.map((f) => f.name)).toEqual(['a.jpg', 'b.jpg']);
    expect(r.error).toContain('c.jpg');
    expect(r.error).toContain('in total');
  });

  it('counts against what is ALREADY staged, not just this pick', async () => {
    // Three picks of 40 would otherwise sail past a 60 cap and be refused by
    // the box after the whole upload — the failure this rule exists to stop.
    const r = await prepareBatch([file('c.jpg', 10)], passthrough, {
      maxImages: 2,
      alreadyPicked: 2,
    });
    expect(r.files).toHaveLength(0);
    expect(r.error).toContain('more than 2 scans');
  });

  it('refuses a non-image by name and keeps the rest of the pick', async () => {
    const r = await prepareBatch(
      [file('a.jpg', 10), file('notes.pdf', 10, 'application/pdf'), file('b.jpg', 10)],
      passthrough,
    );
    expect(r.files.map((f) => f.name)).toEqual(['a.jpg', 'b.jpg']);
    expect(r.error).toContain('notes.pdf');
    expect(r.error).toContain('PNG, JPEG, GIF and WebP');
  });

  it('runs each file through the shared downscale helper', async () => {
    // The batch door must not skip the preparation the chat composer applies —
    // if it did, an image the composer sends fine would be refused here.
    const prepare = vi.fn(async (f: File) => ({
      file: new File([new Uint8Array(50)], f.name, { type: f.type }),
      withinBudget: true,
    }));
    const r = await prepareBatch([file('big.jpg', 9_000_000)], prepare);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(r.files[0].size).toBe(50);
    expect(r.totalBytes).toBe(50);
  });

  it('an empty pick is a no-op, not an error', async () => {
    const r = await prepareBatch([], passthrough);
    expect(r.files).toHaveLength(0);
    expect(r.error).toBeNull();
  });
});

describe('cap agreement with the box', () => {
  // Drift pins. Each constant has a twin in src/alfred/transport/config.py;
  // these fail if this side moves so the move becomes a decision. A python-side
  // move is caught by tests/test_batch_route.py's own cap assertions.
  it('matches DEFAULT_BATCH_MAX_IMAGES', () => {
    expect(MAX_BATCH_IMAGES).toBe(60);
  });
  it('matches DEFAULT_BATCH_MAX_IMAGE_BYTES', () => {
    expect(MAX_BATCH_IMAGE_BYTES).toBe(5 * 1024 * 1024);
  });
  it('matches DEFAULT_BATCH_MAX_TOTAL_BYTES', () => {
    expect(MAX_BATCH_TOTAL_BYTES).toBe(128 * 1024 * 1024);
  });
  it('matches DEFAULT_BATCH_MAX_INSTRUCTION_CHARS', () => {
    expect(MAX_BATCH_INSTRUCTION_CHARS).toBe(4000);
  });
  it('matches ALLOWED_SCAN_MEDIA_TYPES', () => {
    expect([...ALLOWED_BATCH_MEDIA_TYPES].sort()).toEqual([
      'image/gif',
      'image/jpeg',
      'image/png',
      'image/webp',
    ]);
  });
});

describe('friendlyBatchError', () => {
  const say = (code: string) => friendlyBatchError(new ApiError(413, code));

  it('gives the three size caps three different remedies', () => {
    const perImage = say('image_too_large');
    const count = say('too_many_images');
    const total = say('batch_too_large');
    expect(new Set([perImage, count, total]).size).toBe(3);
    expect(perImage).toContain('lower-resolution');
    expect(count).toContain('two batches');
    expect(total).toContain('Split it');
  });

  it('states the real number for each cap', () => {
    expect(say('image_too_large')).toContain('5 MiB');
    expect(say('too_many_images')).toContain('60');
    expect(say('batch_too_large')).toContain('128 MiB');
  });

  it('does not send the operator to re-authenticate over a server credential', () => {
    // The browser never holds the web_batch token, so a logout cannot fix a
    // wrong_peer refusal — same doctrine as the ingest copy.
    const msg = say('wrong_peer');
    expect(msg).toContain('configuration problem on the server');
    expect(msg).toContain("signing in again won't change it");
    // And it names no credential, so a probe learns nothing.
    expect(msg.toLowerCase()).not.toContain('token');
    expect(msg.toLowerCase()).not.toContain('web_batch');
  });

  it('tells an unsigned-in operator to sign in', () => {
    expect(say('invalid_session')).toContain('sign in again');
  });

  it('falls back without pretending to know the cause', () => {
    expect(say('something_new_entirely')).toBe('Something went wrong. Please try again.');
  });

  it('prefers the SERVER’S OWN WORDS over the generic line on an unknown code', () => {
    // The other half of the fallback above, and the one the operator actually
    // needed: the box words its refusals, and answering "Something went wrong"
    // over the top of a sentence it wrote is the honesty gap this closes. The
    // pin above stays exactly as it was — an unknown code with NO detail still
    // gets the generic line — so the two together say the whole rule.
    const detailed = friendlyBatchError(
      new ApiError(422, 'something_new_entirely', 'the campaign for that target is paused'),
    );
    expect(detailed).toBe('the campaign for that target is paused');
  });

  it('does NOT let a detail override a curated cap remedy', () => {
    // A known code keeps its own sentence, which carries a REMEDY the raw
    // detail does not. Preferring the server here would trade "try a
    // lower-resolution scan" for a restatement of the failure.
    const msg = friendlyBatchError(
      new ApiError(413, 'image_too_large', 'image exceeds the per-image byte cap'),
    );
    expect(msg).toContain('lower-resolution');
  });

  it('says the scans were NOT submitted when the instance is unreachable', () => {
    // The operator must know whether to re-pick 30 files. "Try again shortly"
    // alone leaves them guessing whether a partial batch landed.
    expect(say('transport_unreachable')).toContain('were not submitted');
  });
});

describe('submitBatch carries the refusal INTACT', () => {
  // The wire half of the pair above. `friendlyBatchError` preferring the
  // server's words is worth nothing if the throw site never carried them — the
  // two halves have to be pinned separately, because each is green on its own
  // while the operator still reads "Something went wrong."
  function stubFetch(status: number, body: unknown) {
    (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async () => ({
      ok: false,
      status,
      json: async () => body,
    }));
  }

  it('puts the box’s detail on the ApiError, not just the code', async () => {
    stubFetch(422, { error: 'campaign_paused', detail: 'the campaign for that target is paused' });
    await expect(submitBatch('salem', new FormData())).rejects.toMatchObject({
      status: 422,
      code: 'campaign_paused',
      detail: 'the campaign for that target is paused',
    });
  });

  it('and the two halves meet: an unknown code reaches the operator in the box’s words', async () => {
    // END TO END across the seam, because that is the claim being made. Pinning
    // the throw and the mapper separately leaves the join untested, which is the
    // failure shape this project keeps shipping.
    stubFetch(422, { error: 'campaign_paused', detail: 'the campaign for that target is paused' });
    const said = await submitBatch('salem', new FormData()).then(
      () => 'it resolved, which is the bug',
      (e) => friendlyBatchError(e),
    );
    expect(said).toBe('the campaign for that target is paused');
  });

  it('a refusal with no detail still gets the generic line (control)', async () => {
    stubFetch(500, { error: 'something_new_entirely' });
    const said = await submitBatch('salem', new FormData()).then(
      () => 'it resolved, which is the bug',
      (e) => friendlyBatchError(e),
    );
    expect(said).toBe('Something went wrong. Please try again.');
  });
});

describe('batchSuccessMessage', () => {
  const base = {
    batch_id: '20260811-abcd1234',
    images: 12,
    bytes: 500,
    path: 'note/Batch.md',
    instance: 'VERA',
  };

  it('promises processing only when something will process it', () => {
    const queued = batchSuccessMessage({ ...base, status: 'queued' });
    expect(queued).toContain('processing 12 scans');
    expect(queued).toContain('20260811-abcd1234');
  });

  it('says plainly when nothing is set up to process the batch', () => {
    // THE honesty pin. "saved" must not read as "queued": a batch nobody reads
    // sits at 0 of N forever, and promising results would send the operator off
    // to wait for something that cannot arrive.
    const saved = batchSuccessMessage({ ...base, status: 'saved' });
    expect(saved).toContain('nothing is set up to process it');
    expect(saved).not.toContain('processing 12 scans');
  });

  it('reports folded duplicates rather than losing them silently', () => {
    // The operator picked N files and the record will show fewer sections.
    const msg = batchSuccessMessage({ ...base, status: 'queued', duplicates: 2 });
    expect(msg).toContain('2 duplicates');
  });

  it('omits the duplicate line when there were none', () => {
    expect(batchSuccessMessage({ ...base, status: 'queued' })).not.toContain('duplicate');
  });

  it('says "1 scan", not "1 scans"', () => {
    const msg = batchSuccessMessage({ ...base, status: 'queued', images: 1 });
    expect(msg).toContain('1 scan');
    expect(msg).not.toContain('1 scans');
  });
});

describe('buildBatchForm', () => {
  it('uses the field names the box parses', () => {
    // The box matches parts by name: `instruction` as a field, `images` as the
    // file parts. A rename on either side is a silently empty batch.
    const form = buildBatchForm('Read the total.', [file('a.jpg', 10), file('b.jpg', 10)]);
    expect(form.get('instruction')).toBe('Read the total.');
    expect(form.getAll('images')).toHaveLength(2);
  });

  it('omits an empty title rather than sending a blank one', () => {
    expect(buildBatchForm('x', []).get('title')).toBeNull();
    expect(buildBatchForm('x', [], '  ').get('title')).toBeNull();
    expect(buildBatchForm('x', [], 'March invoices').get('title')).toBe('March invoices');
  });
});
