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

export interface UsePushResult {
  status: PushStatus;
  busy: boolean;
  enable: () => void;
  disable: () => void;
}

export function usePush(): UsePushResult {
  const [status, setStatus] = useState<PushStatus>('checking');
  const [busy, setBusy] = useState(false);

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

  const disable = useCallback(() => {
    setBusy(true);
    void (async () => {
      try {
        const reg = await getRegistration();
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (sub) {
          const endpoint = sub.endpoint;
          await sub.unsubscribe().catch(() => undefined);
          await fetch('/api/push/subscribe', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint }),
          }).catch(() => undefined);
        }
        setStatus('off');
      } catch {
        setStatus('error');
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  return { status, busy, enable, disable };
}
