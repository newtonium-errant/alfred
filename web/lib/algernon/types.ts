// Typed mirror of the FROZEN backend /chat contract (src/alfred/web/routes_chat.py).
// These shapes are the cross-agent contract; do not drift them without a sync
// from the team-lead (the backend owns the contract).

export type ChatRole = 'user' | 'assistant';
export type ChatKind = 'text' | 'voice';

// One turn in the flattened web history — matches the backend's
// `_flatten_transcript_for_web` output ({role, text, ts}). `ts` is the turn's
// stamp ('' for pre-stamp records).
export interface HistoryTurn {
  role: ChatRole;
  text: string;
  ts: string;
}

// POST /chat/open  → { session_key }
export interface ChatOpenResponse {
  session_key: string;
}

// POST /chat/turn  { session_key, message, kind } → { reply, session_key }
export interface ChatTurnResponse {
  reply: string;
  session_key: string;
}

// GET /chat/history/{session_key} → { turns: [...] }
export interface ChatHistoryResponse {
  turns: HistoryTurn[];
}

// The backend error envelope: { error: <code>, detail?: <string> }.
export interface ApiErrorBody {
  error: string;
  detail?: string;
}

// A message as the chat UI tracks it: a HistoryTurn plus a stable client-side id
// (React key). No server round-trip needed to render it.
export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  ts: string;
}
