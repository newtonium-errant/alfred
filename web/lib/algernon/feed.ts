import { getJson, postJson } from './http';

// BROWSER-side Feed client → the same-origin BFF (`/api/feed/*`), never the
// transport directly. The BFF holds the `web_feed` peer token and relays. Mirrors
// authClient / the chat client shape (thin wrappers over http.ts getJson/postJson,
// so ApiError.status/.code/.detail surface uniformly to the UI).

export type FeedMode = 'decide' | 'fyi';
export type FeedAttention = 'needs_you' | 'fyi';
/**
 * `deferred` is the store's fifth state (`alfred.feed.store.STATE_DEFERRED`),
 * added here so the closed union stops lying about what the backend can hold.
 *
 * HONEST ABOUT ITS REACH TODAY: the store persists this state and
 * `defer_window_open` reads `deferred_until`, but the action ceiling
 * (`daily_sync/action_router.FEED_ACTIONS`) admits `snooze_*` for
 * `slot_suggestion` alone — so on every other kind the deck's ↑ is still a
 * session-local set-aside that writes nothing, and its copy says so. Wiring the
 * defer verbs per kind is its own lane; this type is the contract half, landed
 * first so that lane cannot be written against a union missing its own state.
 */
export type FeedState = 'open' | 'acted' | 'acked' | 'expired' | 'deferred';

/** Every state the store can hold — the runtime twin of `FeedState`. */
export const FEED_STATES: readonly FeedState[] = [
  'open', 'acted', 'acked', 'expired', 'deferred',
] as const;

/**
 * Narrow an untrusted wire string to `FeedState`.
 *
 * `FeedItem.state` stays a plain `string` on purpose — it is a raw `to_dict`
 * value and typing it as the closed union would be a claim about untrusted
 * input this layer cannot make. This guard is the seam where a consumer opts
 * into the union deliberately, and where an unrecognised future state is
 * observable instead of silently mis-typed.
 */
export function isFeedState(raw: unknown): raw is FeedState {
  return typeof raw === 'string' && (FEED_STATES as readonly string[]).includes(raw);
}

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
  /**
   * The item's own INTERVAL EXTENT (D7) — when the thing itself IS, as opposed
   * to when the feed learned about it (`created_at`, which is provenance).
   *
   * Mirrors `alfred.feed.model.FeedItem.starts_at` / `.ends_at`. The
   * `/feed/items` body already carried these before any client declared them:
   * the BFF relays the transport payload VERBATIM (`pages/api/feed/list.ts`
   * returns `body` untouched) and the browser side is a bare cast, so there is
   * no schema on the response path to strip an unknown key. Nothing on the wire
   * changed when this declaration landed — declaring it IS the whole of the
   * client-side wiring.
   *
   * Both are INDEPENDENTLY optional, and the asymmetry is load-bearing:
   *   - both absent   → the item has no time dimension at all (a merge proposal)
   *   - start, no end → a MOMENT (a 09:30 run), *not* a zero-length span
   *   - start + end   → a real interval (a TAF validity window, a fog corridor)
   *
   * A renderer MUST read `ends_at: null` as "no known end" and never as "ends
   * immediately" — the backend states this as a contract on producers, and
   * drawing a zero-length bar would silently invert it.
   *
   * TWO READERS, ONE FACT, DIFFERENT QUESTIONS — go through one of them rather
   * than parsing these strings at a call site:
   *   - `feedTime.ts::itemExtent` — the timeline's LAYOUT question (where does
   *     this sit on a day axis, and how wide is it).
   *   - `timeExtent.ts::readTimeExtent` — the DISPLAY question (what does this
   *     say to a reader, and how long is it).
   * They disagree on date-only values, and both are right: an all-day item
   * cannot be positioned on an hour axis, but it must still be shown. Whichever
   * you use, the date-only discrimination has to be made on the STRING —
   * `new Date("2026-08-12")` succeeds and invents midnight UTC, turning an
   * all-day event into a zero-length one at 00:00.
   *
   * Optional on this interface (rather than `string | null`) because a payload
   * from a pre-D7 BACKEND legitimately has neither key: the box can be
   * half-deployed — a client that declares them against a transport that does
   * not yet stamp them — and the BFF relays the transport body VERBATIM
   * (`pages/api/feed/list.ts`), so nothing on the response path supplies a
   * default for a key the producer never wrote.
   *
   * NOT a service-worker cache. `sw.js` never intercepts `/api/*` at all — it
   * returns early on that prefix before any cache lookup (sw.js:229, the
   * "never intercept or cache live/session-scoped endpoints" rule) — so no feed
   * payload is ever served from a cache, and a stale cached body cannot be the
   * reason for either key's absence. The conclusion stands; only the mechanism
   * was wrong.
   */
  starts_at?: string | null;
  ends_at?: string | null;
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
  /**
   * `opts.timeoutMs` overrides the 70s browser default. Needed by #62's
   * post-failure verify, whose caller is by definition on a bad connection —
   * the default budget there would leave a spinner up for minutes.
   */
  list(params: FeedListParams = {}, opts: { timeoutMs?: number } = {}): Promise<FeedListResponse> {
    const qs = new URLSearchParams();
    if (params.state) qs.set('state', params.state);
    if (params.mode) qs.set('mode', params.mode);
    if (params.kind) qs.set('kind', params.kind);
    const suffix = qs.toString();
    return getJson<FeedListResponse>(`/api/feed/list${suffix ? `?${suffix}` : ''}`, opts);
  },

  /**
   * Act on one card. `correctionTarget` (#13) is the routine item a rejected
   * completion actually meant — sent only with the routine_match `correct`
   * action, and validated server-side against the vault's live routine items
   * (this layer never decides what is pickable).
   *
   * `contestedSection` (#72 item 4) is the capture-summary heading the operator
   * tapped when contesting an attribution inference — sent only with the
   * `contest` action, and likewise validated server-side (an unrecognised
   * heading files the contest under `unknown` rather than refusing it).
   *
   * Both are trailing optionals rather than an options object because the
   * signature mirrors the router's own keyword-only pair, and the two are never
   * sent together — they belong to different actions on different kinds. A
   * contest passes `undefined` for the target.
   */
  act(
    id: string,
    actionId: string,
    correctionTarget?: string,
    contestedSection?: string,
  ): Promise<FeedActResult> {
    const body: Record<string, string> = { id, action_id: actionId };
    if (correctionTarget) body.correction_target = correctionTarget;
    if (contestedSection) body.contested_section = contestedSection;
    return postJson<FeedActResult>('/api/feed/act', body);
  },
};
