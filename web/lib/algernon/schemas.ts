import { z } from 'zod';

// zod validation at the BFF trust boundary. The browser is untrusted input even
// behind the session cookie — every BFF route parses its body before relaying to
// the transport, so a malformed request is rejected with a 400 at the edge
// rather than forwarded.

// A vault chat message can be long, but an unbounded body is a DoS surface; cap
// it generously. (The engine has its own limits; this is the edge guard.)
export const MAX_MESSAGE_CHARS = 8000;

// POST /api/chat/turn body.
export const chatTurnBodySchema = z.object({
  session_key: z.string().min(1),
  message: z.string().trim().min(1).max(MAX_MESSAGE_CHARS),
  // M1 is text-first; the field is accepted (forward-compat with M2 voice) but
  // defaults to "text". Anything other than "voice" normalises to "text".
  kind: z.enum(['text', 'voice']).optional(),
});

export type ChatTurnBody = z.infer<typeof chatTurnBodySchema>;

// A session_key path/param must be a non-empty string (the backend issues uuids).
export const sessionKeySchema = z.string().min(1).max(200);
