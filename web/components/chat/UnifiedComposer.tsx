import {
  ClipboardEvent,
  DragEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { Textarea } from '../ui/textarea';
import { Input } from '../ui/input';
import { Button } from '../ui/button';
import { VoiceCapture, sttErrorMessage } from './VoiceCapture';
import { subtle } from '../../lib/typography';
import type { ChatKind } from '../../lib/algernon/types';
import type { BatchTarget, BatchTargetsResponse, IngestTarget } from '../../lib/algernon/types';
import {
  MAX_IMAGES_PER_TURN,
  type ImageAttachment,
} from '../../lib/algernon/schemas';
import {
  ALLOWED_BATCH_MEDIA_TYPES,
  MAX_BATCH_IMAGES,
  MAX_BATCH_IMAGE_BYTES,
  MAX_BATCH_INSTRUCTION_CHARS,
  MAX_BATCH_TOTAL_BYTES,
  buildBatchForm,
  friendlyBatchError,
  keyForStagedBatch,
  mib,
  prepareBatch,
  stagedBatchSignature,
  submitBatch,
  type BatchSubmitResponse,
  type StagedBatchKey,
} from '../../lib/algernon/batchSubmit';
import { prepareImageForUpload } from '../../lib/algernon/imagePrepare';
import { collectImageAttachments } from '../../lib/algernon/imageAttach';
import { ApiError } from '../../lib/algernon/http';
import { ingestApi } from '../../lib/algernon/client';
import { sttClient } from '../../lib/algernon/sttClient';
import {
  UNIVERSAL_ACCEPT,
  availableIntents,
  classifyAttachment,
  defaultDocIntent,
  defaultImageIntent,
  intentBlockedReason,
  intentDescription,
  intentLabel,
  matchTarget,
  missingTargetMessage,
  overBatchCapMessage,
  stripExtension,
  unsupportedAttachmentMessage,
  type AttachmentKind,
  type ComposerIntent,
} from '../../lib/algernon/composerRouting';
import {
  FILED_CONTEXT_DEFERRED_MESSAGE,
  PASTED_BODY_RESTORED_MESSAGE,
  PASTED_CHIP_REMOVAL_PROMISE,
  canInline,
  composeTurnMessage,
  fileFromPastedText,
  inlineDocBlock,
  inlineTooLongMessage,
  ingestPayloadFor,
  pasteIngestOfferMessage,
  pasteWantsIngest,
  prepareDoc,
  preparedFromTranscript,
  restorePastedBody,
  runFanout,
  turnOverLimitMessage,
  type FanoutJob,
  type PreparedDoc,
} from '../../lib/algernon/composerFanout';
import { useComposerText } from '../../lib/algernon/useComposerText';

// ONE COMPOSER, THREE PIPELINES UNCHANGED (#97).
//
// The operator's words: "each of these three does similar work with the
// distinction made between what type of file will be received. That's not good
// ux. I want the ingest and scan page ability included in the chat page so
// there's one interface with that instance, and I can send multiple types of
// documents, not to mention discuss them as I do."
//
// So this composer takes everything the three pages jointly accepted, and the
// choice that used to be a choice of PAGE is a visible chip per attachment set.
// Behind it, nothing changed: images ride the conversation on #82's path,
// documents are written by #57's deterministic-create ingest, bulk sets go to
// #83's batch door. This file is ROUTING and UI — no new backend capability, no
// new transport route, and each route's caps and refusal sentences are the ones
// that route already shipped.
//
// FOUR THINGS IT PROMISES, each of which is a pin:
//   1. The chip is visible BEFORE Send and flippable in one tap. Routing is
//      never silent — the chip IS the propose-then-approve surface.
//   2. One attachment's refusal never sinks the others. Every route runs in
//      isolation and reports its own outcome (see `runFanout`).
//   3. A document filed here hands its record path to the same turn, so the
//      reply can read it straight out of the vault — filing and discussing stop
//      being two separate errands.
//   4. Nothing routes to an instance that was not configured for it. A missing
//      target is a refusal naming the instance, never a quiet delivery to
//      whichever one happens to be wired.
//
// It is a COMPONENT, not a page: `pages/chat.tsx` mounts it today and the
// reimagine arc's later shells will remount it as-is.

/** Caption for an attachment-only send — the backend requires a message. */
const ATTACHMENT_ONLY_PLACEHOLDER = '(attachment sent, no message)';

let _chipSeq = 0;
function nextChipId(): string {
  _chipSeq += 1;
  return `c${_chipSeq}`;
}

type ChipState = 'preparing' | 'ready' | 'blocked' | 'sending' | 'done' | 'failed';

interface DocChip {
  id: string;
  kind: Exclude<AttachmentKind, 'image'>;
  file: File;
  intent: ComposerIntent;
  title: string;
  source: string;
  recordType: string;
  prepared: PreparedDoc | null;
  state: ChipState;
  /** The note / refusal / result sentence. Never silently empty. */
  message: string | null;
  detailOpen: boolean;
  /**
   * The pasted text this chip was built from, or null for a file-backed chip.
   *
   * PROVENANCE THAT IS ALSO THE PAYLOAD — deliberately not a boolean beside a
   * filename check. A paste-derived chip holds the ONLY copy of its body, so
   * removing it must put the text back; keeping the marker and the restorable
   * text as one field means they cannot disagree, and there is no filename to
   * match against (a file the operator happened to name "Pasted text.md" is
   * still file-backed and still freely removable).
   */
  pastedBody: string | null;
}

export interface UnifiedComposerProps {
  onSend: (
    text: string,
    kind?: ChatKind,
    images?: ImageAttachment[],
    transcript?: string,
  ) => void;
  disabled?: boolean;
  seedText?: string | null;
  onSeedConsumed?: () => void;
  /** The chat instance selected on the page — every route follows it. */
  instance: string;
  /** Its display label, for copy that must name where something is going. */
  instanceLabel: string;
  /** Injected in tests; defaults to the real BFF clients. */
  submitIngest?: typeof ingestApi.submit;
  submitBatchRequest?: (
    target: string,
    form: FormData,
    idempotencyKey?: string,
  ) => Promise<BatchSubmitResponse>;
  transcribe?: (file: File) => Promise<string>;
  onUnauthenticated?: () => void;
}

async function defaultTranscribe(file: File): Promise<string> {
  const r = await sttClient.transcribe(file);
  return r.transcript || '';
}

export function UnifiedComposer({
  onSend,
  disabled = false,
  seedText = null,
  onSeedConsumed,
  instance,
  instanceLabel,
  submitIngest = ingestApi.submit,
  submitBatchRequest = submitBatch,
  transcribe = defaultTranscribe,
  onUnauthenticated,
}: UnifiedComposerProps) {
  const text = useComposerText({ seedText, onSeedConsumed });

  // Images are ONE chip: the routing question ("discuss these or batch them?")
  // is asked of the SET, because the answer is a property of how many there are.
  const [images, setImages] = useState<File[]>([]);
  const [imageBytes, setImageBytes] = useState(0);
  const [imageIntent, setImageIntent] = useState<ComposerIntent>('discuss');
  const [imageState, setImageState] = useState<ChipState>('ready');
  const [imageMessage, setImageMessage] = useState<string | null>(null);
  const [imageDetailOpen, setImageDetailOpen] = useState(false);

  // Documents and recordings are ONE CHIP EACH: each becomes its own vault
  // record with its own title, so they cannot share a decision.
  const [docs, setDocs] = useState<DocChip[]>([]);

  const [pickError, setPickError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Outcomes of attachments that SUCCEEDED and were therefore cleared from the
  // tray. Without this the good news disappears with the chip: a batch that
  // uploaded fine and a document that filed fine would both leave the screen
  // showing nothing at all, which is indistinguishable from a send that never
  // happened. The failures keep their own chip; the successes keep this line.
  const [results, setResults] = useState<string[]>([]);
  // Record paths filed but not yet named to the assistant — they ride the next
  // turn rather than being dropped (see `composeTurnMessage`'s order of
  // sacrifice). Held across sends on purpose.
  const [filedPaths, setFiledPaths] = useState<string[]>([]);

  // The pasted chunk currently being OFFERED the ingest door — held, not acted
  // on. Null once it has been accepted or declined.
  const [pasteOffer, setPasteOffer] = useState<string | null>(null);
  // Said out loud after a paste-derived chip is removed and its body has landed
  // back in the box. The text reappearing is itself visible, but a destructive-
  // looking action that turns out to be reversible should SAY so rather than
  // leave the operator inferring it.
  const [restoreNotice, setRestoreNotice] = useState<string | null>(null);

  const [ingestTargets, setIngestTargets] = useState<IngestTarget[]>([]);
  const [batchTargets, setBatchTargets] = useState<BatchTarget[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // The idempotency key for the currently staged batch (#100). A ref, because a
  // FAILED send keeps its chip and its files — so pressing Send again must
  // resend the SAME key rather than mint a second batch. The chip-level
  // mitigation shipped with #97 (a done chip clears and is never resent) does
  // not cover this: the response was lost, so the chip never became done.
  const batchKeyRef = useRef<StagedBatchKey | null>(null);

  // Which instances accept documents / bulk scans. Metadata only; a failure
  // leaves the lists empty, which reads as "not configured here" — the same
  // fail-closed answer the standalone pages give, never a silent success.
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const r = await ingestApi.targets();
        if (active) setIngestTargets(r.targets);
      } catch {
        /* stays empty → the File chip refuses with words naming the instance */
      }
    })();
    void (async () => {
      try {
        const res = await fetch('/api/batch/targets');
        const body: BatchTargetsResponse = res.ok ? await res.json() : { targets: [] };
        if (active) setBatchTargets(body?.targets ?? []);
      } catch {
        /* same */
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const patchDoc = useCallback((id: string, patch: Partial<DocChip>) => {
    setDocs((prev) => prev.map((d) => (d.id === id ? { ...d, ...patch } : d)));
  }, []);

  // --- picking --------------------------------------------------------------

  const addImages = useCallback(
    async (picked: File[]) => {
      // `prepareBatch` is the SUPERSET preparation: it runs the same
      // downscale-then-step-down both routes use, and enforces the count,
      // per-image and total-byte caps with #83's own named sentences. Running it
      // here — before the intent is even known — means one preparation per file
      // and one place those caps are stated, whichever chip the operator ends up
      // choosing. The conversation route's per-turn count cap (4) is a separate
      // question, answered by the chip's own availability rather than by a cap
      // that would refuse a perfectly good batch.
      const prepared = await prepareBatch(picked, prepareImageForUpload, {
        alreadyPicked: images.length,
        alreadyBytes: imageBytes,
      });
      if (prepared.files.length > 0) {
        setImages((prev) => {
          const next = [...prev, ...prepared.files];
          // The default is recomputed on every change to the SET, because it is
          // a function of the count — five pictures are a batch whether they
          // arrived together or one at a time.
          setImageIntent(defaultImageIntent(next.length));
          return next;
        });
        setImageBytes(prepared.totalBytes);
        setImageState('ready');
        setImageMessage(null);
      }
      if (prepared.error) setPickError(prepared.error);
    },
    [images.length, imageBytes],
  );

  const addDoc = useCallback(
    async (
      file: File,
      kind: Exclude<AttachmentKind, 'image'>,
      /**
       * Set ONLY by the paste path, which is the one caller whose chip holds
       * the sole copy of its body. Both production call sites are explicit:
       * `addFiles` omits it (file-backed, freely removable) and
       * `acceptPasteOffer` passes the body (removal restores it).
       */
      pastedBody: string | null = null,
    ) => {
      const id = nextChipId();
      const name = file.name.trim();
      setDocs((prev) => [
        ...prev,
        {
          id,
          kind,
          file,
          pastedBody,
          intent: defaultDocIntent(),
          // Filename as the title, extension stripped, and as the source
          // verbatim — the same defaults the ingest form fills in, so a document
          // dropped here needs no typing at all to be filed sensibly.
          title: stripExtension(name),
          source: name,
          recordType: 'document',
          prepared: null,
          state: 'preparing',
          message:
            kind === 'audio'
              ? 'Transcribing the recording…'
              : 'Reading the file…',
          detailOpen: false,
        },
      ]);

      if (kind === 'audio') {
        try {
          const transcript = await transcribe(file);
          const doc = preparedFromTranscript(transcript);
          if (!doc.ok) {
            patchDoc(id, { prepared: null, state: 'blocked', message: doc.message });
            return;
          }
          patchDoc(id, {
            prepared: doc,
            state: 'ready',
            // The transcript is shown as a NOTE with its length: a recording
            // whose transcription came back near-empty is a fact the operator
            // needs before it becomes a vault record, not after.
            message: `Transcribed — ${doc.body.length.toLocaleString()} characters. Open the details to check the title before filing.`,
          });
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) onUnauthenticated?.();
          patchDoc(id, { state: 'blocked', message: sttErrorMessage(e) });
        }
        return;
      }

      const doc = await prepareDoc(file);
      if (!doc.ok) {
        patchDoc(id, { prepared: null, state: 'blocked', message: doc.message });
        return;
      }
      patchDoc(id, { prepared: doc, state: 'ready', message: doc.note });
    },
    [patchDoc, transcribe, onUnauthenticated],
  );

  const addFiles = useCallback(
    async (list: File[]) => {
      if (list.length === 0) return;
      setPickError(null);
      setSendError(null);
      // A new attachment starts a new send — last send's receipts stop being
      // about what is now on screen, and neither is a restore that has already
      // happened.
      setResults([]);
      setRestoreNotice(null);
      const pickedImages: File[] = [];
      const rejected: string[] = [];
      for (const file of list) {
        const kind = classifyAttachment(file);
        if (kind === 'image') pickedImages.push(file);
        else if (kind) void addDoc(file, kind);
        else rejected.push(file.name);
      }
      if (pickedImages.length > 0) await addImages(pickedImages);
      // Named, not counted: "some files could not be added" is the
      // operator-facing form of silence.
      if (rejected.length > 0) setPickError(unsupportedAttachmentMessage(rejected[0]));
    },
    [addDoc, addImages],
  );

  // Accepting the offer moves the pasted body OUT of the message and into a
  // document chip. It becomes an ordinary attachment from here — same chip,
  // same Details fields, same ingest route, same refusals — and like any
  // attachment, removing the chip discards it.
  const acceptPasteOffer = useCallback(() => {
    if (pasteOffer === null) return;
    setPasteOffer(null);
    setRestoreNotice(null);
    // Take the pasted chunk back out of the message — that chunk only, so
    // whatever the operator typed around it stays exactly where it was. Read
    // from the CURRENT value rather than a snapshot taken at paste time: a
    // paste that replaced a selection should not resurrect what it replaced.
    text.setValue(text.value.replace(pasteOffer, ''));
    // The body rides ALONG to the chip, which is what makes removal reversible.
    void addDoc(fileFromPastedText(pasteOffer), 'doc', pasteOffer);
  }, [pasteOffer, text, addDoc]);

  const declinePasteOffer = useCallback(() => setPasteOffer(null), []);

  const removeImages = useCallback(() => {
    setImages([]);
    setImageBytes(0);
    setImageState('ready');
    setImageMessage(null);
    setPickError(null);
  }, []);

  // THE INVERSE OF ACCEPT. Accepting moved the body message→chip; removing
  // moves it chip→message. For a file-backed chip this does nothing, because
  // the file on disk is still the real copy and the chip was only a reference —
  // but a paste-derived chip holds the ONLY copy, and discarding it with the
  // same one-line filter would destroy the operator's own text behind a button
  // that looks identical to the harmless case.
  const removeDoc = useCallback(
    (id: string) => {
      const doomed = docs.find((d) => d.id === id);
      if (doomed?.pastedBody != null) {
        text.setValue(restorePastedBody(text.value, doomed.pastedBody));
        setRestoreNotice(PASTED_BODY_RESTORED_MESSAGE);
      }
      setDocs((prev) => prev.filter((d) => d.id !== id));
    },
    [docs, text],
  );

  // --- intent flips ---------------------------------------------------------

  const flipImageIntent = useCallback(
    (intent: ComposerIntent) => {
      const blocked = intentBlockedReason('image', intent, { count: images.length });
      if (blocked) {
        setImageMessage(blocked);
        return;
      }
      setImageIntent(intent);
      setImageState('ready');
      setImageMessage(null);
    },
    [images.length],
  );

  const flipDocIntent = useCallback(
    (doc: DocChip, intent: ComposerIntent) => {
      const blocked = intentBlockedReason(doc.kind === 'audio' ? 'audio' : 'doc', intent, {
        filename: doc.file.name,
      });
      if (blocked) {
        patchDoc(doc.id, { message: blocked });
        return;
      }
      // Quoting into a message is bounded by the message itself, so the check
      // happens at the FLIP — the operator learns the document is too long
      // while choosing, not after pressing Send.
      const prepared = doc.prepared;
      if (intent === 'discuss' && prepared?.ok && !canInline(prepared)) {
        const chars = prepared.format === 'text' ? prepared.body.length : 0;
        patchDoc(doc.id, { message: inlineTooLongMessage(doc.file.name, chars) });
        return;
      }
      patchDoc(doc.id, {
        intent,
        state: doc.state === 'failed' || doc.state === 'blocked' ? 'ready' : doc.state,
        message: doc.prepared?.ok ? (doc.prepared.note ?? null) : null,
      });
    },
    [patchDoc],
  );

  // --- sending --------------------------------------------------------------

  // The offer is shown only while the chunk it describes is STILL IN THE BOX,
  // and that is DERIVED rather than stored. Two reasons, both learned here:
  // an effect that cleared the offer raced the paste itself (the offer is set
  // in the paste event, one render before the textarea's value commits, so the
  // effect saw an empty box and withdrew an offer that had just been made);
  // and once the text is gone — sent, deleted, or edited down — a panel still
  // quoting its character count would be describing a paste that no longer
  // exists. Deriving answers both without a second source of truth. The length
  // test short-circuits the common exits before the substring scan.
  const showPasteOffer =
    pasteOffer !== null &&
    text.value.length >= pasteOffer.length &&
    text.value.includes(pasteOffer);

  const hasAttachments = images.length > 0 || docs.length > 0;
  // The message doubles as the batch instruction when a batch is staged, so an
  // image-only batch still needs words — that is not an empty send.
  const canSend =
    !busy && !disabled && (text.value.trim().length > 0 || hasAttachments);

  async function submit() {
    if (!canSend) return;
    setSendError(null);

    const typed = text.value.trim();
    const batchStaged = images.length > 0 && imageIntent === 'batch';

    // 1. Build the jobs. Every refusal here marks ONE chip and leaves the others
    //    alone — this is where partial-failure honesty starts, before any
    //    request is made.
    const jobs: FanoutJob[] = [];
    let imageBlocked = false;

    if (batchStaged) {
      const target = matchTarget(instance, batchTargets);
      if (!target) {
        setImageState('blocked');
        setImageMessage(missingTargetMessage('batch', instanceLabel));
        imageBlocked = true;
      } else if (images.length > MAX_BATCH_IMAGES) {
        setImageState('blocked');
        setImageMessage(overBatchCapMessage(images.length));
        imageBlocked = true;
      } else if (!typed) {
        setImageState('blocked');
        setImageMessage(
          'Write what should be read from every scan — your message is the instruction sent with each one.',
        );
        imageBlocked = true;
      } else if (typed.length > MAX_BATCH_INSTRUCTION_CHARS) {
        setImageState('blocked');
        setImageMessage(
          `That instruction is ${(typed.length - MAX_BATCH_INSTRUCTION_CHARS).toLocaleString()} characters over the ${MAX_BATCH_INSTRUCTION_CHARS.toLocaleString()}-character limit. It is sent with every scan, so it needs to be short.`,
        );
        imageBlocked = true;
      } else {
        // Same staged set ⇒ same key. The signature covers the target, the
        // instruction and the files, so editing any of them mints a new key and
        // the edit is honoured rather than being replaced by a replay of the
        // first receipt.
        const staged = keyForStagedBatch(
          batchKeyRef.current,
          stagedBatchSignature({
            target: target.name,
            instruction: typed,
            files: images,
          }),
        );
        batchKeyRef.current = staged;
        jobs.push({
          id: 'images',
          route: 'batch',
          target: target.name,
          instruction: typed,
          files: images,
          idempotencyKey: staged.key,
        });
      }
    }

    const fileDocs = docs.filter((d) => d.intent === 'file' && d.prepared?.ok);
    for (const doc of fileDocs) {
      const target = matchTarget(instance, ingestTargets);
      if (!target) {
        patchDoc(doc.id, {
          state: 'blocked',
          message: missingTargetMessage('file', instanceLabel),
        });
        continue;
      }
      if (!doc.title.trim() || !doc.source.trim()) {
        patchDoc(doc.id, {
          state: 'blocked',
          message: 'A title and source are both required.',
        });
        continue;
      }
      jobs.push({
        id: doc.id,
        route: 'file',
        payload: ingestPayloadFor({
          target: target.name,
          recordType: doc.recordType,
          title: doc.title,
          source: doc.source,
          doc: doc.prepared as Extract<PreparedDoc, { ok: true }>,
        }),
      });
    }

    // 2. What rides the conversation.
    const inlineDocs = docs.filter((d) => d.intent === 'discuss' && d.prepared?.ok);
    const inlineBlocks = inlineDocs.map((d) =>
      inlineDocBlock(
        d.file.name,
        d.prepared?.ok && d.prepared.format === 'text' ? d.prepared.body : '',
      ),
    );
    // A batch's instruction is NOT also a chat message: it is words about the
    // scans, addressed to the extraction, and sending it to the assistant as
    // well would put a page-reading instruction into the conversation.
    const turnText = batchStaged ? '' : typed;

    // 3. Refuse an over-long turn BEFORE anything is submitted, so a refusal
    //    never arrives with half the attachments already gone.
    const dryRun = composeTurnMessage({ text: turnText, inlineBlocks });
    if (dryRun.overLimit) {
      setSendError(turnOverLimitMessage(dryRun.message.length));
      return;
    }

    setBusy(true);
    try {
      // 4. The carried images, prepared by the conversation route's own gate.
      //    `prepare` is a pass-through: these files were already downscaled at
      //    pick time by the shared helper, and re-encoding a second time would
      //    cost fidelity for nothing. The BYTE gate still runs, as does the mime
      //    allowlist and the per-turn count — each with its own sentence.
      let carried: ImageAttachment[] = [];
      if (images.length > 0 && imageIntent === 'discuss') {
        const collected = await collectImageAttachments(images, {
          prepare: async (f, maxBytes) => ({ file: f, withinBudget: f.size <= maxBytes }),
        });
        carried = collected.accepted;
        if (collected.error) {
          setImageState('failed');
          setImageMessage(collected.error);
        }
      }

      // 5. Run the vault + batch routes, each isolated from the others.
      if (jobs.length > 0) {
        for (const job of jobs) {
          if (job.id === 'images') setImageState('sending');
          else patchDoc(job.id, { state: 'sending', message: 'Filing…' });
        }
      }
      const { outcomes, filedPaths: freshPaths } = await runFanout(jobs, {
        submitIngest,
        submitBatch: ({ target, instruction, files, title, idempotencyKey }) =>
          submitBatchRequest(
            target,
            buildBatchForm(instruction, files, title),
            idempotencyKey,
          ),
      });

      let batchSucceeded = false;
      const receipts: string[] = [];
      for (const [id, outcome] of Object.entries(outcomes)) {
        // A SUCCESS moves to the receipts line, because its chip is about to be
        // cleared; a FAILURE stays on its chip, which is retained for the retry.
        if (outcome.status === 'done') receipts.push(outcome.message);
        if (id === 'images') {
          setImageState(outcome.status);
          setImageMessage(outcome.status === 'done' ? null : outcome.message);
          batchSucceeded = outcome.status === 'done';
          // RETIRE the key once its submission has been answered. A key answers
          // for ONE batch; holding it would mean a later re-pick of the same
          // scans with the same instruction rebuilt the same signature and was
          // handed the old receipt — a second batch asked for and not created.
          if (batchSucceeded) batchKeyRef.current = null;
        } else {
          patchDoc(id, {
            state: outcome.status,
            message: outcome.status === 'done' ? null : outcome.message,
          });
        }
      }
      if (receipts.length > 0) setResults(receipts);

      // 6. The turn — sent LAST so it can name what was just filed.
      const allPaths = [...filedPaths, ...freshPaths];
      const wantsTurn =
        turnText.length > 0 || carried.length > 0 || inlineBlocks.length > 0;
      let carriedPaths = allPaths;
      if (wantsTurn) {
        const composed = composeTurnMessage({
          text: turnText,
          inlineBlocks,
          filedPaths: allPaths,
        });
        const message = composed.message || ATTACHMENT_ONLY_PLACEHOLDER;
        const transcript = text.takeTranscript();
        if (carried.length > 0 || transcript) {
          onSend(message, text.kind, carried.length > 0 ? carried : undefined, transcript);
        } else {
          onSend(message, text.kind);
        }
        carriedPaths = composed.filedCarried ? [] : allPaths;
        if (!composed.filedCarried && allPaths.length > 0) {
          setSendError(FILED_CONTEXT_DEFERRED_MESSAGE);
        }
        text.reset();
      } else if (batchSucceeded) {
        // The instruction went with the scans; the box is cleared because the
        // words were used, not discarded. A FAILED batch keeps them so the
        // retry does not begin with retyping the instruction.
        text.reset();
      }
      setFiledPaths(carriedPaths);

      // 7. Clear what succeeded; KEEP what did not, so a retry is one tap and
      //    the operator never has to re-pick a file the instance refused.
      if (carried.length > 0 || batchSucceeded) removeImages();
      setDocs((prev) =>
        prev.filter((d) => {
          if (d.intent === 'discuss') return false; // it rode the message
          return outcomes[d.id]?.status !== 'done';
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const pasted = Array.from(e.clipboardData?.files ?? []);
    if (pasted.length > 0) {
      e.preventDefault();
      void addFiles(pasted);
      return;
    }
    // A long TEXT paste is offered the ingest door — the design's third route
    // in. The paste is NOT intercepted: it lands in the box exactly as it
    // always has, and the offer appears beside it. Converting it silently
    // would be the routing-without-consent this whole surface is shaped
    // against, and it would do it to the operator's own words rather than to
    // a file they chose to attach.
    const pastedText = e.clipboardData?.getData('text') ?? '';
    if (pasteWantsIngest(pastedText)) setPasteOffer(pastedText);
  }

  function onDrop(e: DragEvent<HTMLTextAreaElement>) {
    const dropped = Array.from(e.dataTransfer?.files ?? []);
    if (dropped.length > 0) {
      e.preventDefault();
      void addFiles(dropped);
    }
  }

  // --- render ---------------------------------------------------------------

  const chipStateClass = (state: ChipState): string => {
    if (state === 'failed' || state === 'blocked') return 'border-danger bg-danger-bg';
    if (state === 'done') return 'border-honeydew-300 bg-honeydew-100';
    return 'border-honeydew-300 bg-white';
  };

  function IntentButtons({
    kind,
    current,
    onFlip,
    idBase,
    count,
    filename,
    disabledAll,
  }: {
    kind: AttachmentKind;
    current: ComposerIntent;
    onFlip: (i: ComposerIntent) => void;
    idBase: string;
    count?: number;
    filename?: string;
    disabledAll: boolean;
  }) {
    const offered = availableIntents(kind, { count, filename });
    // Every intent the type can EVER take is rendered, so the operator sees the
    // choice exists; the ones this set cannot take are disabled and say why when
    // pressed. A choice that silently disappears teaches the operator it was
    // never possible.
    const all: ComposerIntent[] =
      kind === 'image' ? ['discuss', 'batch'] : ['file', 'discuss'];
    return (
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Where this goes">
        {all.map((intent) => {
          const available = offered.includes(intent);
          const active = current === intent;
          return (
            <button
              key={intent}
              type="button"
              data-testid={`${idBase}-intent-${intent}`}
              aria-pressed={active}
              disabled={disabledAll}
              onClick={() => onFlip(intent)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                active
                  ? 'bg-honeydew-600 text-white'
                  : available
                    ? 'border border-honeydew-300 bg-white text-honeydew-700 hover:bg-honeydew-50'
                    : 'border border-honeydew-200 bg-honeydew-50 text-honeydew-400'
              }`}
            >
              {intentLabel(intent)}
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="unified-composer">
      <VoiceCapture
        idPrefix="composer-voice"
        disabled={disabled || busy}
        insertDirectly
        // One audio door, not two: a recording uploaded through the universal
        // Attach becomes a chip that can be filed OR discussed, while this
        // control stays what it has always been — dictation straight into the
        // box. Two upload affordances with different outcomes is the confusion
        // this whole surface exists to remove.
        showFileUpload={false}
        onTranscript={text.appendTranscript}
      />

      {images.length > 0 && (
        <section
          data-testid="unified-chip-images"
          aria-label="Attached images"
          className={`flex flex-col gap-2 rounded-xl border p-3 ${chipStateClass(imageState)}`}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-semibold text-honeydew-800">
              {images.length === 1 ? '1 image' : `${images.length} images`}
            </span>
            <div className="flex items-center gap-2">
              <IntentButtons
                kind="image"
                current={imageIntent}
                onFlip={flipImageIntent}
                idBase="unified-images"
                count={images.length}
                disabledAll={disabled || busy}
              />
              <button
                type="button"
                data-testid="unified-images-detail"
                aria-expanded={imageDetailOpen}
                onClick={() => setImageDetailOpen((o) => !o)}
                className="text-xs font-semibold text-honeydew-700 underline"
              >
                {imageDetailOpen ? 'Hide details' : 'Details'}
              </button>
              <button
                type="button"
                data-testid="unified-images-remove"
                disabled={disabled || busy}
                onClick={removeImages}
                className="text-xs font-semibold text-danger underline"
              >
                Remove
              </button>
            </div>
          </div>

          <p className={subtle} data-testid="unified-images-description">
            {intentDescription(imageIntent, instanceLabel)}
          </p>

          {imageDetailOpen && (
            <div className="flex flex-col gap-1" data-testid="unified-images-detail-panel">
              {/* The caps, stated BEFORE the operator does the scanning work —
                  an operator about to scan thirty pages should learn the limit
                  before doing it, not after. */}
              <p className={subtle} data-testid="unified-images-limits">
                Up to {MAX_BATCH_IMAGES} scans per batch, {mib(MAX_BATCH_IMAGE_BYTES)} MiB
                each and {mib(MAX_BATCH_TOTAL_BYTES)} MiB in total;{' '}
                {MAX_IMAGES_PER_TURN} at most when discussing them in a message.{' '}
                {ALLOWED_BATCH_MEDIA_TYPES.map((t) => t.replace('image/', '').toUpperCase()).join(', ')}.
                Large scans are resized automatically before upload.
              </p>
              {imageIntent === 'batch' && (
                <p className={subtle} data-testid="unified-images-instruction-note">
                  Your message is the instruction sent with every scan — it is not
                  also posted to the conversation.
                </p>
              )}
              <ul className="flex flex-col gap-0.5" data-testid="unified-images-list">
                {images.map((f, i) => (
                  <li key={`${f.name}-${i}`} className="truncate font-mono text-xs">
                    {f.name}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {imageMessage && (
            <p
              role={imageState === 'failed' || imageState === 'blocked' ? 'alert' : 'status'}
              data-testid="unified-images-message"
              className={
                imageState === 'failed' || imageState === 'blocked'
                  ? 'text-sm text-danger'
                  : `text-sm ${imageState === 'done' ? 'text-honeydew-800' : 'text-honeydew-600'}`
              }
            >
              {imageMessage}
            </p>
          )}
        </section>
      )}

      {docs.map((doc, index) => (
        <section
          key={doc.id}
          data-testid={`unified-chip-doc-${index}`}
          aria-label={`Attachment ${doc.file.name}`}
          className={`flex flex-col gap-2 rounded-xl border p-3 ${chipStateClass(doc.state)}`}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="truncate text-sm font-semibold text-honeydew-800">
              {doc.file.name}
            </span>
            <div className="flex items-center gap-2">
              <IntentButtons
                kind={doc.kind}
                current={doc.intent}
                onFlip={(i) => flipDocIntent(doc, i)}
                idBase={`unified-doc-${index}`}
                filename={doc.file.name}
                disabledAll={disabled || busy || doc.state === 'preparing'}
              />
              <button
                type="button"
                data-testid={`unified-doc-${index}-detail`}
                aria-expanded={doc.detailOpen}
                onClick={() => patchDoc(doc.id, { detailOpen: !doc.detailOpen })}
                className="text-xs font-semibold text-honeydew-700 underline"
              >
                {doc.detailOpen ? 'Hide details' : 'Details'}
              </button>
              <button
                type="button"
                data-testid={`unified-doc-${index}-remove`}
                disabled={disabled || busy}
                onClick={() => removeDoc(doc.id)}
                className="text-xs font-semibold text-danger underline"
              >
                Remove
              </button>
            </div>
          </div>

          <p className={subtle} data-testid={`unified-doc-${index}-description`}>
            {intentDescription(doc.intent, instanceLabel)}
          </p>

          {/* Said BEFORE the destructive-looking action, not after it. Remove
              on this chip is not the same button it is on a file-backed one —
              here it returns the text rather than dropping a reference — and
              the operator should know that while deciding, not discover it by
              risking their own words. Derived from the chip's provenance rather
              than folded into `message`, which the intent flip resets. */}
          {doc.pastedBody !== null && (
            <p className={subtle} data-testid={`unified-doc-${index}-paste-promise`}>
              {PASTED_CHIP_REMOVAL_PROMISE}
            </p>
          )}

          {/* The ingest fields, folded away behind Details with sensible
              defaults already filled in — the operator files a document without
              typing anything, and can still fix the title before it is written. */}
          {doc.detailOpen && doc.intent === 'file' && (
            <div className="flex flex-col gap-2" data-testid={`unified-doc-${index}-detail-panel`}>
              <label className="flex flex-col gap-1 text-xs font-semibold text-honeydew-700">
                Title
                <Input
                  data-testid={`unified-doc-${index}-title`}
                  value={doc.title}
                  maxLength={300}
                  disabled={disabled || busy}
                  onChange={(e) => patchDoc(doc.id, { title: e.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-semibold text-honeydew-700">
                Source
                <Input
                  data-testid={`unified-doc-${index}-source`}
                  value={doc.source}
                  maxLength={500}
                  disabled={disabled || busy}
                  onChange={(e) => patchDoc(doc.id, { source: e.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-semibold text-honeydew-700">
                Record type
                <select
                  data-testid={`unified-doc-${index}-type`}
                  value={doc.recordType}
                  disabled={disabled || busy}
                  onChange={(e) => patchDoc(doc.id, { recordType: e.target.value })}
                  className="rounded-xl border border-honeydew-300 bg-white px-3 py-2 text-sm text-honeydew-800"
                >
                  {(matchTarget(instance, ingestTargets)?.recordTypes ?? ['document']).map(
                    (t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ),
                  )}
                </select>
              </label>
            </div>
          )}

          {doc.message && (
            <p
              role={doc.state === 'failed' || doc.state === 'blocked' ? 'alert' : 'status'}
              data-testid={`unified-doc-${index}-message`}
              className={
                doc.state === 'failed' || doc.state === 'blocked'
                  ? 'text-sm text-danger'
                  : `text-sm ${doc.state === 'done' ? 'text-honeydew-800' : 'text-honeydew-600'}`
              }
            >
              {doc.message}
            </p>
          )}
        </section>
      ))}

      {/* What the last send actually did, for the attachments that are no longer
          on screen because they worked. Every line names its record. */}
      {results.length > 0 && (
        <ul data-testid="unified-results" role="status" className="flex flex-col gap-1">
          {results.map((line, i) => (
            <li
              key={i}
              data-testid={`unified-result-${i}`}
              className="rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800"
            >
              {line}
            </li>
          ))}
        </ul>
      )}

      {/* A record filed but not yet named to the assistant. Explicit rather than
          held quietly, so "it knows about it" is never a guess. */}
      {filedPaths.length > 0 && (
        <p className={subtle} data-testid="unified-filed-context" role="status">
          Your next message will mention{' '}
          {filedPaths.length === 1
            ? `the record just filed (${filedPaths[0]})`
            : `${filedPaths.length} records just filed`}
          .
        </p>
      )}

      {restoreNotice && (
        <p
          role="status"
          data-testid="unified-paste-restored"
          className="rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800"
        >
          {restoreNotice}
        </p>
      )}

      {/* The long-paste offer. A PROPOSAL, not a routing: the text is still in
          the box behind this, and declining leaves it there. Calm styling
          rather than danger-red on purpose — nothing has gone wrong, and a
          paste bigger than a message is a normal thing to have done. */}
      {showPasteOffer && pasteOffer !== null && (
        <section
          role="status"
          data-testid="unified-paste-offer"
          aria-label="Long paste"
          className="flex flex-col gap-2 rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800"
        >
          <p data-testid="unified-paste-offer-message">
            {pasteIngestOfferMessage(pasteOffer.length)}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              data-testid="unified-paste-offer-accept"
              disabled={disabled || busy}
              onClick={acceptPasteOffer}
            >
              File it as a document
            </Button>
            <button
              type="button"
              data-testid="unified-paste-offer-decline"
              onClick={declinePasteOffer}
              className="text-xs font-semibold text-honeydew-700 underline"
            >
              Keep it in the message
            </button>
          </div>
        </section>
      )}

      {pickError && (
        <p role="alert" data-testid="unified-pick-error" className="text-sm text-danger">
          {pickError}
        </p>
      )}

      {sendError && (
        <p role="alert" data-testid="unified-send-error" className="text-sm text-danger">
          {sendError}
        </p>
      )}

      <form
        className="flex items-end gap-2"
        data-testid="unified-composer-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          ref={fileInputRef}
          data-testid="unified-file-input"
          type="file"
          accept={UNIVERSAL_ACCEPT}
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) void addFiles(Array.from(e.target.files));
            // Reset so re-picking the SAME file fires change again.
            e.target.value = '';
          }}
        />
        <Button
          type="button"
          variant="outline"
          data-testid="unified-attach"
          aria-label="Attach a file"
          disabled={disabled || busy}
          onClick={() => fileInputRef.current?.click()}
        >
          Attach
        </Button>
        <Textarea
          data-testid="unified-input"
          aria-label="Message"
          placeholder={
            images.length > 0 && imageIntent === 'batch'
              ? 'What should be read from every scan?'
              : 'Message…'
          }
          rows={1}
          value={text.value}
          disabled={disabled || busy}
          onChange={(e) => text.setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onDrop={onDrop}
          className="max-h-40 min-h-[44px]"
        />
        <Button type="submit" data-testid="unified-send" disabled={!canSend}>
          {busy ? 'Sending…' : 'Send'}
        </Button>
      </form>
    </div>
  );
}
