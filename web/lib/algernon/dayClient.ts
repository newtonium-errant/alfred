import { getJson, postJson } from './http';
import type { ContactRule, ContactSurface, DayState } from './contactRouter';

// BROWSER-side contact-router client → the same-origin BFF (`/api/day/*`), never
// the transport directly. The BFF relays the httpOnly session cookie; the
// transport peer-pins and resolves the identity. Same thin-wrapper shape as
// authClient / feed.ts, so `ApiError.status/.code` surfaces uniformly.

export interface ContactRecorded {
  contact_id: string;
  /**
   * FALSE when the instance has no contact store wired. A 200 with
   * `recorded: false` is the honest answer, not an error: the surface really did
   * open, and only the logging of it was skipped. The caller keeps the
   * distinction so "the router isn't learning" is visible rather than assumed.
   */
  recorded: boolean;
}

export interface OverrideRecorded {
  recorded: boolean;
  patterns_surfaced: number;
}

export const dayApi = {
  state: (): Promise<DayState> => getJson<DayState>('/api/day/state'),

  contact: (rule: ContactRule, surface: ContactSurface): Promise<ContactRecorded> =>
    postJson<ContactRecorded>('/api/day/contact', { rule, surface }),

  override: (contactId: string, surface: ContactSurface): Promise<OverrideRecorded> =>
    postJson<OverrideRecorded>('/api/day/override', {
      contact_id: contactId,
      surface,
    }),
};
