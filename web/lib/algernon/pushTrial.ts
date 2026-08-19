import { mkdir, appendFile, readFile } from 'fs/promises';
import { dirname } from 'path';

// SERVER-ONLY. The VAPID push-delivery TRIAL — a measurement instrument with a
// seven-day life, not a product.
//
// THE QUESTION IT ANSWERS. Push is wired end to end (VAPID, a subscription
// store, a healthy poller, a subscribed phone) and has never sent a single
// notification, so nobody knows whether iOS PWA push actually ARRIVES on the
// operator's phone, or how late. Until that is known, no feature can be built on
// the doorbell. This fires known pushes at known times and records what came
// back.
//
// WHY IT DOES NOT RIDE THE EXISTING POLICY ENGINE. `pushPolicy.isPushEligible`
// decides which FEED ITEM may ring the doorbell — it takes a `FeedItem`. A trial
// push is not a feed item and must not become one: manufacturing feed rows to
// carry test sends would put synthetic cards in the operator's real deck for him
// to dismiss. So the trial reuses everything that matters (the subscription
// store, the VAPID sender, the poller's heartbeat) and adds only its own
// schedule + ledger.
//
// ## THE THREE-STATE RULE — the reason this file is careful
//
// The trial exists because "I didn't get a notification" is ambiguous. An
// instrument that reproduces that ambiguity is worthless, so a slot is never
// just delivered/undelivered. It is one of FOUR states, and the two failure
// shapes must never be conflated:
//
//   scheduled   — its time passed and NO send was attempted. The instrument was
//                 down (the poller is a Next singleton that resumes on the next
//                 push-route hit after a restart). This says NOTHING about
//                 delivery and must never be counted as a miss.
//   send_failed — the send itself errored. An instrument-side fact, also not a
//                 delivery datum.
//   sent        — the push left the server and no receipt came back. GENUINELY
//                 UNKNOWN: it may never have arrived, or it may have arrived and
//                 gone untapped. Only the operator can collapse this, which is
//                 what `reconcile` is for.
//   received    — the operator tapped it; latency is real.
//
// Reporting `sent` as "undelivered" would manufacture exactly the false signal
// the trial was commissioned to eliminate.
//
// ## Why the whole schedule is materialised up front
//
// Every slot is written to the ledger as a `scheduled` row when the trial
// starts. Two reasons. (1) It is what makes "never sent" VISIBLE — a due slot
// with no sent row is an instrument outage, and you cannot see the absence of a
// send against a schedule that was never written down. (2) It keeps the schedule
// in ONE place: the status surface (`alfred push-trial status`, Python) reads
// rows and joins them on push_id; it never re-derives when a send was due. Two
// implementations of one schedule in two languages is the divergent-constants
// trap, and this sidesteps it rather than pinning it.

export const TRIAL_DAYS = 7;
export const TRIAL_SENDS_PER_DAY = 3;

// Minutes past local midnight for the three daily windows — morning, midday,
// evening. Deliberately spread: the question is whether delivery differs by time
// of day (background-app-refresh throttling on iOS is diurnal), so clustering
// the sends would answer a different question than the one asked.
export const TRIAL_WINDOW_BASES_MIN = [8 * 60, 13 * 60, 19 * 60];
// Deterministic spread INSIDE each window, so sends are not all at :00 (a phone
// on a tidy hour boundary invites coincidence with other wakeups).
export const TRIAL_WINDOW_SPREAD_MIN = 90;

// `ruling` is the one row type this side never WRITES — it is appended by
// `alfred push-trial rule`, the operator's after-the-fact answer about a slot
// that was sent and never tapped. Named here anyway because both runtimes must
// READ every type either one writes: an unlisted type would be a whole class of
// ledger row this side silently ignored, and `dueSlots` in particular must keep
// treating a ruled slot as already attempted (it filters on sent/send_failed,
// so it does — pinned rather than assumed).
// `concluded` and `cancelled` are the EARLY END (below). `concluded` is written
// from the operator's side (`alfred push-trial conclude`), like `ruling`;
// `cancelled` is written HERE, one row per slot the conclusion spends.
export type TrialRowType =
  | 'scheduled'
  | 'sent'
  | 'send_failed'
  | 'receipt'
  | 'ruling'
  | 'concluded'
  | 'cancelled';

/** The trial-level stop marker. Written once; the sender checks for it before
 * every send. */
export const ROW_CONCLUDED = 'concluded';
/** A slot the conclusion retired without sending. */
export const ROW_CANCELLED = 'cancelled';

/**
 * The `push_id` on a `concluded` row.
 *
 * The row is about the TRIAL, not a slot, but the field is present and empty
 * rather than absent — both readers require `push_id` to be a string and treat a
 * row without one as MALFORMED (the Python reader counts it into `skipped`,
 * which is its corruption signal). An always-present field carrying an empty
 * value is the shape the house rule asks for; a conditionally-present one would
 * make "the trial was stopped" indistinguishable from "the ledger is damaged".
 */
export const CONCLUDED_PUSH_ID = '';

export interface TrialRow {
  type: TrialRowType;
  push_id: string;
  /** ISO — when the slot was due. Present on `scheduled` rows. */
  due_ts?: string;
  /** ISO — when the send left the server. Present on sent/send_failed. */
  sent_ts?: string;
  /** ISO — when the operator tapped. Present on `receipt`. */
  received_ts?: string;
  error?: string;
  /** `arrived` | `missed` — the operator's verdict. Present on `ruling`. */
  verdict?: string;
  /** ISO — when he ruled. Present on `ruling`. */
  ruled_ts?: string;
  /** ISO — when the trial was concluded. Present on `concluded`. */
  concluded_ts?: string;
  /** ISO — when the slot was retired unsent. Present on `cancelled`. */
  cancelled_ts?: string;
  /** WHY, in the operator's words. Present on `concluded` and copied onto every
   * `cancelled` row it spends, so a slot read on its own still says why it never
   * fired instead of merely that it didn't. */
  reason?: string;
}

export interface TrialConfig {
  /** Local midnight of trial day 1, as epoch ms. */
  startMs: number;
  days: number;
}

function trialPath(): string {
  // Matches pushStore's convention exactly (env override, ./data default) —
  // this ledger is a sibling of push_subs.jsonl and belongs beside it.
  return process.env.ALFRED_WEB_PUSH_TRIAL || './data/push_trial.jsonl';
}

/** True when the operator has switched the trial on (independent of PUSH_ENABLED,
 * which gates the sender the trial rides). */
export function isTrialEnabled(): boolean {
  return (process.env.PUSH_TRIAL_ENABLED || '').trim().toLowerCase() === 'true';
}

/**
 * The trial window, or null when unset/unparseable. `PUSH_TRIAL_START` is an ISO
 * datetime naming LOCAL MIDNIGHT OF DAY 1 (the box runs America/Halifax); slots
 * are offsets from it, so the schedule is reproducible from the config alone and
 * a reader never has to guess a timezone.
 */
export function readTrialConfig(): TrialConfig | null {
  if (!isTrialEnabled()) return null;
  const raw = (process.env.PUSH_TRIAL_START || '').trim();
  if (!raw) return null;
  const startMs = Date.parse(raw);
  if (Number.isNaN(startMs)) return null;
  const days = Number.parseInt((process.env.PUSH_TRIAL_DAYS || '').trim(), 10);
  return { startMs, days: Number.isFinite(days) && days > 0 ? days : TRIAL_DAYS };
}

/** Deterministic per-slot offset inside its window. Not cryptographic — it only
 * has to be stable and spread, so the same config always yields the same
 * schedule and the ledger can be regenerated for comparison. */
export function slotJitterMin(dayIndex: number, windowIndex: number): number {
  const h = (dayIndex * 31 + windowIndex * 17 + 7) % TRIAL_WINDOW_SPREAD_MIN;
  return h;
}

export function slotPushId(dayIndex: number, windowIndex: number): string {
  return `trial-d${dayIndex + 1}-w${windowIndex + 1}`;
}

export interface TrialSlot {
  push_id: string;
  dueMs: number;
}

/** The full schedule for a trial config — pure, total, and the ONE definition. */
export function buildSchedule(cfg: TrialConfig): TrialSlot[] {
  const slots: TrialSlot[] = [];
  for (let d = 0; d < cfg.days; d += 1) {
    for (let w = 0; w < TRIAL_WINDOW_BASES_MIN.length; w += 1) {
      const minutes = TRIAL_WINDOW_BASES_MIN[w] + slotJitterMin(d, w);
      slots.push({
        push_id: slotPushId(d, w),
        dueMs: cfg.startMs + d * 86_400_000 + minutes * 60_000,
      });
    }
  }
  return slots;
}

/** The trial push's payload. Same three-key shape as the real doorbell (the
 * lock-screen privacy rule is not relaxed for a test), with the push_id carried
 * in the deep link so the tap can be attributed to the slot that caused it. */
export function trialPayloadFor(pushId: string): { title: string; kind: string; url: string } {
  return {
    title: 'Algernon delivery test',
    kind: 'push_trial',
    url: `/feed?push=${encodeURIComponent(pushId)}`,
  };
}

// --- ledger I/O --------------------------------------------------------------

async function appendRow(path: string, row: TrialRow): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  // JSON.stringify per line — a value bearing a newline is escaped and cannot
  // forge a second record (the store-forge pin, same as pushStore).
  await appendFile(path, JSON.stringify(row) + '\n', 'utf8');
}

/** Every parseable ledger row, in file order. A malformed line is skipped rather
 * than failing the read: a half-written ledger must still report what it knows. */
export async function readTrialRows(path = trialPath()): Promise<TrialRow[]> {
  let raw: string;
  try {
    raw = await readFile(path, 'utf8');
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') return [];
    throw e;
  }
  const out: TrialRow[] = [];
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    try {
      const o = JSON.parse(line) as TrialRow;
      if (o && typeof o.push_id === 'string' && typeof o.type === 'string') out.push(o);
    } catch {
      /* skip a malformed line */
    }
  }
  return out;
}

export async function appendTrialRow(row: TrialRow, path = trialPath()): Promise<void> {
  await appendRow(path, row);
}

// --- the pass ----------------------------------------------------------------

export interface TrialDeps {
  readRows: () => Promise<TrialRow[]>;
  appendRow: (row: TrialRow) => Promise<void>;
  sendTrialPush: (payload: string) => Promise<number>;
  now: () => number;
  config: () => TrialConfig | null;
}

export interface TrialResult {
  reason: 'disabled' | 'materialised' | 'no_due_slots' | 'sent' | 'concluded';
  materialised: number;
  due: number;
  sent: number;
  failed: number;
  /** Slots retired unsent by an early conclusion. Always present (0 when none),
   * so a reader never has to tell "no cancellations" apart from "this build
   * predates cancellation". */
  cancelled: number;
}

/** Slots whose time has come and which have not been attempted yet.
 *
 * "Attempted" means a sent OR send_failed row exists — a failed send is NOT
 * retried. Retrying would silently turn one scheduled observation into several
 * sends at unplanned times, which corrupts the very thing being measured (a
 * delivery at 19:47 attributed to a 19:07 slot is not the datum it claims). The
 * failure is recorded and the slot is spent. */
export function dueSlots(schedule: TrialSlot[], rows: TrialRow[], nowMs: number): TrialSlot[] {
  return schedule.filter((s) => s.dueMs <= nowMs && !spentIds(rows).has(s.push_id));
}

/**
 * Slots that are SPENT — already attempted, or retired by a conclusion. One
 * definition, because "has this slot had its turn?" is asked in two places and
 * two spellings of it would drift.
 *
 * `cancelled` belongs here for the same reason `send_failed` does: the slot is
 * over. Omitting it would make the poller re-cancel every remaining slot on
 * every 60-second heartbeat, writing a fresh row each time — an append-only
 * ledger growing without bound to record a decision that was made once.
 */
export function spentIds(rows: TrialRow[]): Set<string> {
  return new Set(
    rows
      .filter((r) => r.type === 'sent' || r.type === 'send_failed' || r.type === ROW_CANCELLED)
      .map((r) => r.push_id),
  );
}

/**
 * The conclusion row, if the operator ended the trial early.
 *
 * FIRST one wins: the trial stopped when he said so, and a second marker cannot
 * un-stop it or move the moment it happened. (A `ruling` is the opposite — later
 * wins, because he is allowed to correct a recollection. A conclusion is an
 * EVENT, not an opinion, so it does not get re-decided.)
 */
export function concludedRow(rows: TrialRow[]): TrialRow | null {
  return rows.find((r) => r.type === ROW_CONCLUDED) ?? null;
}

/**
 * One trial pass, run on the poller's heartbeat. Materialises the schedule on
 * first run, then sends whatever is due.
 *
 * A pass that sends nothing still says so (ILB): silence from a seven-day
 * instrument is indistinguishable from a stopped one, which is the failure this
 * whole feature exists to make visible.
 */
export async function runTrialOnce(deps: TrialDeps): Promise<TrialResult> {
  const cfg = deps.config();
  if (!cfg) {
    return { reason: 'disabled', materialised: 0, due: 0, sent: 0, failed: 0, cancelled: 0 };
  }
  const schedule = buildSchedule(cfg);
  let rows = await deps.readRows();

  // Materialise once — idempotent on the presence of any scheduled row.
  let materialised = 0;
  if (!rows.some((r) => r.type === 'scheduled')) {
    for (const slot of schedule) {
      await deps.appendRow({
        type: 'scheduled',
        push_id: slot.push_id,
        due_ts: new Date(slot.dueMs).toISOString(),
      });
      materialised += 1;
    }
    rows = await deps.readRows();
    console.log(`[push:trial] schedule materialised slots=${materialised} days=${cfg.days}`);
  }

  // THE EARLY END. Checked before anything is sent, because the whole value of a
  // stop verb is that the next send does not happen.
  //
  // It retires EVERY unspent slot, not only the ones currently due. A check that
  // only guarded the due ones would leave future slots sitting in the ledger as
  // `pending` — a status surface would go on promising sends that will never
  // come, and the operator would have to keep the poller's internals in his head
  // to know that. One pass, and every remaining slot states its own outcome.
  //
  // CANCELLED, NOT SKIPPED. A silently skipped slot is indistinguishable from an
  // instrument that stopped — which is the exact ambiguity this module was
  // commissioned to destroy (see the three-state rule above). The cancelled row
  // carries the reason so the ledger says why, not just that.
  const concluded = concludedRow(rows);
  if (concluded) {
    const spent = spentIds(rows);
    const remaining = schedule.filter((s) => !spent.has(s.push_id));
    const at = new Date(deps.now()).toISOString();
    for (const slot of remaining) {
      await deps.appendRow({
        type: ROW_CANCELLED,
        push_id: slot.push_id,
        cancelled_ts: at,
        reason: concluded.reason || 'trial concluded early',
      });
    }
    // ILB on BOTH branches: the pass that does the cancelling says how many it
    // retired, and every later pass says it is standing down and why. A concluded
    // trial that simply went quiet would look exactly like a dead poller.
    console.log(
      remaining.length > 0
        ? `[push:trial] concluded — cancelled remaining slots=${remaining.length} reason=${concluded.reason || '(none given)'}`
        : `[push:trial] concluded — no slots left to cancel (instrument alive, nothing owed)`,
    );
    return {
      reason: 'concluded',
      materialised,
      due: 0,
      sent: 0,
      failed: 0,
      cancelled: remaining.length,
    };
  }

  const due = dueSlots(schedule, rows, deps.now());
  if (due.length === 0) {
    console.log(
      `[push:trial] ran due=0 (nothing scheduled for now — instrument alive, no send owed)`,
    );
    return {
      reason: materialised ? 'materialised' : 'no_due_slots',
      materialised, due: 0, sent: 0, failed: 0, cancelled: 0,
    };
  }

  let sent = 0;
  let failed = 0;
  for (const slot of due) {
    const payload = JSON.stringify(trialPayloadFor(slot.push_id));
    const at = new Date(deps.now()).toISOString();
    try {
      const recipients = await deps.sendTrialPush(payload);
      await deps.appendRow({ type: 'sent', push_id: slot.push_id, sent_ts: at });
      sent += 1;
      console.log(`[push:trial] sent push_id=${slot.push_id} recipients=${recipients} at=${at}`);
    } catch (e) {
      const err = (e as Error)?.message ?? 'unknown';
      await deps.appendRow({ type: 'send_failed', push_id: slot.push_id, sent_ts: at, error: err });
      failed += 1;
      console.warn(`[push:trial] send_failed push_id=${slot.push_id} err=${err}`);
    }
  }
  return { reason: 'sent', materialised, due: due.length, sent, failed, cancelled: 0 };
}
