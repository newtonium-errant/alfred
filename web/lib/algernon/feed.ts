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
export interface FeedActResult {
  ok: boolean;
  status: string;
  detail: string;
  id: string;
  action_id: string;
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

  act(id: string, actionId: string): Promise<FeedActResult> {
    return postJson<FeedActResult>('/api/feed/act', { id, action_id: actionId });
  },
};
