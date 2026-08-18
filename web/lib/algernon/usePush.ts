import { useCallback, useEffect, useState } from 'react';

// CLIENT push subscription flow (B4). Checks browser support + server VAPID
// config, then exposes enable()/disable(). Best-effort and defensive — every
// failure resolves to a status, never a throw. The heavy browser APIs
// (serviceWorker / PushManager / Notification) make this hard to unit-test, so
// the load-bearing logic lives server-side (routes, notifier, payload) which IS
// tested; this hook is thin glue.

export type PushStatus =
  | 'checking' // still probing support + server config
  | 'unsupported' // this browser can't do web push
  | 'unconfigured' // server has no VAPID keys (feature inert)
  | 'denied' // the user blocked notifications
  | 'off' // supported + configured, not subscribed
  | 'on' // subscribed
  | 'error';

// VAPID public key (base64url) → the applicationServerKey BufferSource expects.
// Constructed over an explicit ArrayBuffer so the type is Uint8Array<ArrayBuffer>
// (the bare numeric-length ctor widens to ArrayBufferLike, which the DOM
// PushManager types reject).
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const buffer = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

function pushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

async function getRegistration(): Promise<ServiceWorkerRegistration | null> {
  try {
    return (await navigator.serviceWorker.ready) ?? null;
  } catch {
    return null;
  }
}

/**
 * The doorbell is still live HERE — the browser refused to unsubscribe.
 *
 * The status stays 'on' with this, because it is: pushes will still arrive at
 * this browser. Saying 'off' would be the toggle lying about the one thing it
 * exists to report.
 */
export const PUSH_STILL_ON_MESSAGE =
  'This browser is still subscribed — the switch didn’t take. Try again, or turn notifications off for this site in your browser settings.';

/**
 * The browser IS unsubscribed, but the box was never told.
 *
 * Not an error, and not silence either. The endpoint is dead, so nothing will
 * ring here — but the box still holds a row for it and will keep trying until
 * something prunes it. The operator's switch did what it says; the cleanup did
 * not, and that is worth one sentence rather than a cheerful 'off'.
 */
export const PUSH_SERVER_NOT_TOLD_MESSAGE =
  'Notifications are off on this browser, but the server wasn’t told — it may keep a stale subscription for a while. Nothing will ring here.';

export interface UsePushResult {
  status: PushStatus;
  busy: boolean;
  /**
   * What the last disable actually did, when it was not a clean success.
   *
   * Null on a clean result. Deliberately NOT folded into `status`: the two
   * answer different questions — `status` is "will this browser ring", the note
   * is "did everything the switch implies actually happen" — and collapsing
   * them is how 'off' came to mean both "you are unsubscribed" and "we tried".
   */
  note: string | null;
  enable: () => void;
  disable: () => void;
}

export function usePush(): UsePushResult {
  const [status, setStatus] = useState<PushStatus>('checking');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // Probe support + server config + existing subscription on mount.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (!pushSupported()) {
        if (!cancelled) setStatus('unsupported');
        return;
      }
      try {
        const res = await fetch('/api/push/public-key');
        if (res.status === 503) {
          if (!cancelled) setStatus('unconfigured');
          return;
        }
        if (!res.ok) {
          if (!cancelled) setStatus('error');
          return;
        }
        const reg = await getRegistration();
        const existing = reg ? await reg.pushManager.getSubscription() : null;
        if (cancelled) return;
        if (Notification.permission === 'denied') setStatus('denied');
        else setStatus(existing ? 'on' : 'off');
      } catch {
        if (!cancelled) setStatus('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const enable = useCallback(() => {
    setBusy(true);
    // A previous disable's note is about a subscription that no longer matters
    // once a new one is being made — leaving it up would have the surface
    // reporting the last teardown beside a fresh switch-on.
    setNote(null);
    void (async () => {
      try {
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          setStatus(perm === 'denied' ? 'denied' : 'off');
          return;
        }
        const keyRes = await fetch('/api/push/public-key');
        if (!keyRes.ok) {
          setStatus(keyRes.status === 503 ? 'unconfigured' : 'error');
          return;
        }
        const { publicKey } = (await keyRes.json()) as { publicKey: string };
        const reg = await getRegistration();
        if (!reg) {
          setStatus('error');
          return;
        }
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        });
        const res = await fetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub.toJSON()),
        });
        setStatus(res.ok ? 'on' : 'error');
      } catch {
        setStatus('error');
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  // TWO STEPS, TWO ANSWERS, AND THE SWITCH REPORTS BOTH.
  //
  // This used to run both steps under `.catch(() => undefined)` and then set
  // 'off' UNCONDITIONALLY. Either half could fail — or both — and the toggle
  // said the same calm word every time. The bad case is not hypothetical: an
  // unsubscribe that fails leaves this browser SUBSCRIBED while the switch
  // reads off, so the next push arrives at a device the operator believes they
  // silenced, and nothing on screen ever suggested otherwise.
  //
  // The two failures are genuinely different facts and must not collapse:
  //   * the BROWSER did not unsubscribe → it will still ring here → 'on';
  //   * the browser unsubscribed but the BOX was not told → nothing rings here,
  //     but a stale row survives server-side → 'off', with a note.
  // Pruning that stale row is the box's business (a server-side GC question,
  // boarded separately); what belongs here is not pretending it isn't there.
  const disable = useCallback(() => {
    setBusy(true);
    setNote(null);
    void (async () => {
      try {
        const reg = await getRegistration();
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (!sub) {
          // Already unsubscribed — nothing to undo and nothing to report.
          setStatus('off');
          return;
        }
        const endpoint = sub.endpoint;

        let localGone: boolean;
        try {
          // `unsubscribe()` RESOLVES FALSE when there was nothing to remove, so
          // the boolean is read rather than discarded: a resolved promise is
          // not the same as a successful unsubscribe, and treating it as one is
          // how the old code reported success it had never observed.
          localGone = await sub.unsubscribe();
        } catch {
          localGone = false;
        }

        let serverTold = false;
        try {
          const res = await fetch('/api/push/subscribe', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint }),
          });
          serverTold = res.ok;
        } catch {
          serverTold = false;
        }

        if (!localGone) {
          // The DELETE is still attempted above even in this branch: if the box
          // drops the row, a subscription this browser could not shed at least
          // stops being fed. Saying 'off' here would be the lie.
          setStatus('on');
          setNote(PUSH_STILL_ON_MESSAGE);
          return;
        }
        setStatus('off');
        setNote(serverTold ? null : PUSH_SERVER_NOT_TOLD_MESSAGE);
      } catch {
        setStatus('error');
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  return { status, busy, note, enable, disable };
}
