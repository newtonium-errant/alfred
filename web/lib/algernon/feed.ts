import { getJson, postJson } from './http';

// BROWSER-side Feed client → the same-origin BFF (`/api/feed/*`), never the
// transport directly. The BFF holds the `web_feed` peer token and relays. Mirrors
// authClient / the chat client shape (thin wrappers over http.ts getJson/postJson,
// so ApiError.status/.code/.detail surface uniformly to the UI).

export type FeedMode = 'decide' | 'fyi';
export type FeedAttention = 'needs_you' | 'fyi';
export type FeedState = 'open' | 'acted' | 'acked' | 'expired';

// The FeedItem shape mirrors the backend model.FeedItem to_dict (Feed Phase A).
// `evidence` / `source_ref` are RAW to_dict payloads with NO schema guarantee —
// render them defensively (see components/feed/evidence helpers): missing keys,
// arbitrary nesting, and untrusted display strings (escape everything).
export interface FeedItem {
  id: string;
  kind: string;
  instance: string;
  title: string;
  mode: string;
  attention: string;
  evidence: Record<string, unknown>;
  actions: Array<Record<string, unknown>>;
  state: string;
  // The VERB stamped on the last state event (C2): "accept" | "done" | undefined
  // (legacy / pre-C2 store items). Discriminates the state=acted overload — a C2
  // accept and a C1b completion both set state=acted, so the verb (not the stale
  // evidence.candidate flag) decides the stage. Absent → legacy DONE.
  acted_action?: string | null;
  created_at: string;
  acted_at: string | null;
  expires_at: string | null;
  source_ref: Record<string, unknown>;
}

export interface FeedListResponse {
  items: FeedItem[];
  count: number;
}

// POST /api/feed/act result (the B1 ActResult.to_dict shape). `status` is the
// machine code (acted | acked | already_acted | stale_item | invalid_action |
// error); `detail` is the human line (the resolver's own message where it has one).
//
// `render` is the C2 slot-ACCEPT committed payload — present ONLY on an accept
// success (the router sets it in _dispatch_slot_confirm and to_dict emits it only
// when non-None; ABSENT on already_acted / invalid_action / error / every other
// action). The FE gates its optimistic candidate→planned flip on render being
// PRESENT (never on status alone) — absent render → refetch/reconcile, never a lie.
export interface FeedActRender {
  tier: number;
  name: string;
  committed: boolean;
}

export interface FeedActResult {
  ok: boolean;
  status: string;
  detail: string;
  id: string;
  action_id: string;
  render?: FeedActRender;
}

export interface FeedListParams {
  state?: string;
  mode?: string;
  kind?: string;
}

export const feedApi = {
  list(params: FeedListParams = {}): Promise<FeedListResponse> {
    const qs = new URLSearchParams();
    if (params.state) qs.set('state', params.state);
    if (params.mode) qs.set('mode', params.mode);
    if (params.kind) qs.set('kind', params.kind);
    const suffix = qs.toString();
    return getJson<FeedListResponse>(`/api/feed/list${suffix ? `?${suffix}` : ''}`);
  },

  /**
   * Act on one card. `correctionTarget` (#13) is the routine item a rejected
   * completion actually meant — sent only with the routine_match `correct`
   * action, and validated server-side against the vault's live routine items
   * (this layer never decides what is pickable).
   */
  act(id: string, actionId: string, correctionTarget?: string): Promise<FeedActResult> {
    const body: Record<string, string> = { id, action_id: actionId };
    if (correctionTarget) body.correction_target = correctionTarget;
    return postJson<FeedActResult>('/api/feed/act', body);
  },
};
