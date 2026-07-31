import { mkdir, readFile, rename, writeFile } from 'fs/promises';
import { dirname } from 'path';

// SERVER-ONLY. JSONL persistence for push subscriptions + the poller's seen-id
// set. Paths are SERVER-FIXED (env override or default), never client-derived.
// Every line is JSON.stringify'd → a value bearing a newline is escaped and can't
// forge a second record (the composer-log forge-pin, applied here too).
//
// Subscriptions are a read-modify-write set (dedupe-on-endpoint + remove-on-
// delete) rather than a pure append, so both stores rewrite atomically
// (tmp → rename) to avoid a torn file on crash.

export interface StoredSubscription {
  user: string;
  endpoint: string;
  p256dh: string;
  auth: string;
  at: string;
}

function subsPath(): string {
  return process.env.ALFRED_WEB_PUSH_SUBS || './data/push_subs.jsonl';
}
function seenPath(): string {
  return process.env.ALFRED_WEB_PUSH_SEEN || './data/push_seen.jsonl';
}

async function readLines(path: string): Promise<string[]> {
  try {
    const raw = await readFile(path, 'utf8');
    return raw.split('\n').filter((l) => l.trim().length > 0);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') return [];
    throw e;
  }
}

async function atomicWrite(path: string, content: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.tmp`;
  await writeFile(tmp, content, 'utf8');
  await rename(tmp, path);
}

/** All stored subscriptions (malformed lines skipped defensively). */
export async function readSubscriptions(): Promise<StoredSubscription[]> {
  const out: StoredSubscription[] = [];
  for (const line of await readLines(subsPath())) {
    try {
      const o = JSON.parse(line) as Record<string, unknown>;
      if (typeof o.endpoint === 'string' && typeof o.p256dh === 'string' && typeof o.auth === 'string') {
        out.push({
          user: typeof o.user === 'string' ? o.user : '',
          endpoint: o.endpoint,
          p256dh: o.p256dh,
          auth: o.auth,
          at: typeof o.at === 'string' ? o.at : '',
        });
      }
    } catch {
      /* skip a malformed line rather than fail the whole read */
    }
  }
  return out;
}

/** Add (or replace, deduped on endpoint) a subscription. */
export async function addSubscription(sub: StoredSubscription): Promise<void> {
  const existing = await readSubscriptions();
  const kept = existing.filter((s) => s.endpoint !== sub.endpoint);
  kept.push(sub);
  await atomicWrite(subsPath(), kept.map((s) => JSON.stringify(s)).join('\n') + '\n');
}

/** Remove the subscription with this endpoint. Returns whether one was removed. */
export async function removeSubscription(endpoint: string): Promise<boolean> {
  const existing = await readSubscriptions();
  const kept = existing.filter((s) => s.endpoint !== endpoint);
  if (kept.length === existing.length) return false;
  await atomicWrite(subsPath(), kept.length ? kept.map((s) => JSON.stringify(s)).join('\n') + '\n' : '');
  return true;
}

/** The set of feed-item ids already pushed (so the doorbell never rings twice). */
export async function readSeenIds(): Promise<Set<string>> {
  const s = new Set<string>();
  for (const line of await readLines(seenPath())) {
    try {
      const id = JSON.parse(line);
      if (typeof id === 'string') s.add(id);
    } catch {
      /* skip malformed */
    }
  }
  return s;
}

/** Persist the (already bounded) seen-id set — one JSON-escaped id per line. */
export async function writeSeenIds(ids: string[]): Promise<void> {
  await atomicWrite(seenPath(), ids.length ? ids.map((id) => JSON.stringify(id)).join('\n') + '\n' : '');
}
