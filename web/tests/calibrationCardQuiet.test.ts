import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { isNeedsYouItem } from '../lib/algernon/feedNeedsYou';
import {
  CALIBRATION_KIND,
  calibrationActionable,
  contestableItem,
  hasSuggestedChoice,
  isDeckCandidate,
} from '../lib/algernon/feedConstants';
import { withServedActions } from './helpers/servedActions';
import type { FeedItem } from '../lib/algernon/feed';

// R4 — THE VOICE-CALIBRATION CARD IS QUIET, AND STAYS QUIET.
//
// WHY THIS TEST IS IN TYPESCRIPT AND READS PYTHON. The card's silence is a
// cross-language property whose switch is on the far side: `KIND_DEFAULTS` in
// `src/alfred/feed/model.py` seeds `calibration` as `(fyi, fyi)`, and the two
// predicates that could make it loud both live here. A Python test asserting
// "the tuple says fyi" would only restate the line it guards.
//
// TWO INDEPENDENT WAYS THIS CARD COULD START RINGING, and both are pinned
// because they fail in opposite ways and neither is visible from the other:
//
//   1. `mode: 'decide'` — `isNeedsYouItem` is `attention === 'needs_you' ||
//      mode === 'decide'`, and the push poller fetches on that predicate with
//      no kind allowlist. There is no decide/fyi combination that stays quiet.
//   2. A SUGGESTED CHOICE GROUP — `isDeckCandidate` is `mode === 'decide' ||
//      hasSuggestedChoice`, so giving BOTH calibration verbs a shared `group`
//      in ACTION_META would deal this card onto the DECK. This is the
//      non-obvious half: it is a change in the Python PRESENTATION table,
//      nowhere near the seed, and it moves the card to a needs-you surface
//      without touching mode or attention at all.
//
//      MEASURED, not assumed: a group on ONE verb is inert
//      (`holdChoicesForVerb` requires `family.length >= 2`), and both
//      directions are pinned below so the boundary is a fact in the suite.
//
// The mode/attention below come FROM THE PYTHON SOURCE rather than from a
// literal typed here, so downgrading the seed reds this file. A hand-written
// `mode: 'fyi'` fixture could never do that — it would keep passing while the
// operator's phone woke him up.

const MODEL_PY = join(process.cwd(), '..', 'src', 'alfred', 'feed', 'model.py');

/** Top-level `NAME = "value"` constants, so a tuple of symbols can be resolved. */
function pythonStringConstants(source: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /^([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']+)["']\s*$/gm;
  for (const m of source.matchAll(re)) out[m[1]] = m[2];
  return out;
}

/** The `(mode, attention)` Python actually assigns to `calibration`. */
function kindDefaultsForCalibration(): { kind: string; mode: string; attention: string } {
  const source = readFileSync(MODEL_PY, 'utf8');
  const constants = pythonStringConstants(source);
  const kind = constants.KIND_CALIBRATION;
  if (!kind) throw new Error('KIND_CALIBRATION not found in model.py');

  const entry = new RegExp(
    `^\\s*(?:KIND_CALIBRATION|["']${kind}["'])\\s*:\\s*\\(\\s*([A-Za-z0-9_"']+)\\s*,\\s*([A-Za-z0-9_"']+)\\s*\\)`,
    'm',
  ).exec(source);
  if (!entry) throw new Error(`no KIND_DEFAULTS entry for ${kind} in model.py`);

  const resolve = (token: string): string => {
    const literal = /^["'](.+)["']$/.exec(token);
    if (literal) return literal[1];
    const value = constants[token];
    if (!value) throw new Error(`cannot resolve ${token} in model.py`);
    return value;
  };
  return { kind, mode: resolve(entry[1]), attention: resolve(entry[2]) };
}

const PY = kindDefaultsForCalibration();

/**
 * A calibration card as the daily-sync producer emits one.
 *
 * `withServedActions` stamps the verbs the SERVER really advertises (read from
 * `servedActions.json`, which is generated from `FEED_ACTIONS` + `ACTION_META`)
 * — so `hasSuggestedChoice` below is answered by production's real verb list,
 * not by one this file invented. That is what makes hazard 2 checkable here.
 */
function calibrationCard(): FeedItem {
  return withServedActions({
    id: 'calibration:cal-af67d2840822',
    kind: PY.kind,
    instance: 'Salem',
    title: 'Calibration: Prefers bottom-line-up-front answers.',
    // FROM PYTHON — see the header. Not a literal.
    mode: PY.mode,
    attention: PY.attention,
    evidence: {
      proposal_id: 'cal-af67d2840822',
      subsection: 'Communication Style',
      bullet: 'Prefers bottom-line-up-front answers.',
    },
    actions: [],
    state: 'open',
    created_at: '2026-08-21T09:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: { producer: 'daily_sync' },
  });
}

describe('the calibration card is quiet', () => {
  it('is seeded fyi/fyi in the Python source', () => {
    // The premise, asserted rather than assumed — every claim below rests on it.
    expect(PY.kind).toBe(CALIBRATION_KIND);
    expect(PY.mode).toBe('fyi');
    expect(PY.attention).toBe('fyi');
  });

  it('does not ring the phone (hazard 1: mode)', () => {
    expect(isNeedsYouItem(calibrationCard())).toBe(false);
  });

  it('POSITIVE CONTROL: the same card WOULD ring if seeded decide', () => {
    // Without this, "does not ring" is indistinguishable from a predicate that
    // can never return true for anything.
    const loud = { ...calibrationCard(), mode: 'decide' };
    expect(isNeedsYouItem(loud)).toBe(true);
  });

  it('is not dealt to the deck (hazard 2: no suggested choice group)', () => {
    const card = calibrationCard();
    expect(hasSuggestedChoice(card)).toBe(false);
    expect(isDeckCandidate(card)).toBe(false);
  });

  it('POSITIVE CONTROL: a grouped FAMILY OF TWO would make it deck-eligible', () => {
    // This is the pin that catches someone adding `"group"` to the calibration
    // verbs in ACTION_META. The mutation is in a Python presentation table far
    // from the seed, and it would deal this card to the deck without touching
    // mode or attention at all.
    //
    // TWO verbs, not one, and the correction is worth recording because the
    // first draft of this test grouped only the affirm and went RED: a single
    // grouped verb is INERT. `holdChoicesForVerb` requires `family.length >= 2`
    // ("a family of one is no family"), and the dynamic arm needs a >= 2-entry
    // `evidence.moc_choices`, which a calibration card never carries. So the
    // real hazard is a grouped PAIR, and the guard sentence in `model.py` /
    // `action_router.py` says exactly that rather than the stronger, false
    // "add a group to either verb".
    const card = calibrationCard();
    const grouped: FeedItem = {
      ...card,
      actions: (card.actions ?? []).map((a: Record<string, unknown>) =>
        a.gesture === 'affirm' || a.gesture === 'reject'
          ? { ...a, group: 'calibration' }
          : a,
      ),
    } as FeedItem;
    expect(hasSuggestedChoice(grouped)).toBe(true);
    expect(isDeckCandidate(grouped)).toBe(true);
  });

  it('a group on ONE verb alone is inert — the measured boundary', () => {
    // The other side of the correction above, pinned so the boundary is a fact
    // in the suite rather than a sentence in a comment. If `holdChoicesForVerb`
    // ever drops its `>= 2` clause, THIS test reds and the guard sentences
    // above become too weak — which is the direction that needs telling.
    const card = calibrationCard();
    const oneGrouped: FeedItem = {
      ...card,
      actions: (card.actions ?? []).map((a: Record<string, unknown>) =>
        a.gesture === 'affirm' ? { ...a, group: 'calibration' } : a,
      ),
    } as FeedItem;
    expect(hasSuggestedChoice(oneGrouped)).toBe(false);
    expect(isDeckCandidate(oneGrouped)).toBe(false);
  });

  it('offers its own affordance, and only on its own kind', () => {
    expect(calibrationActionable(calibrationCard())).toBe(true);
    // The per-kind gate, from the other side: the affordance must not appear on
    // a neighbouring quiet card.
    const weather = { ...calibrationCard(), kind: 'weather' };
    expect(calibrationActionable(weather)).toBe(false);
  });

  it('ATTRIBUTION IS UNCHANGED — the blast-radius control', () => {
    // The ruling that authorised this affordance rested on both gates being
    // per-kind. This asserts that held in practice: an attribution row still
    // answers the contest predicate and does NOT answer the calibration one, so
    // it gained no calibration buttons.
    const attribution = {
      ...calibrationCard(),
      id: 'attribution:person/Andrew.md|inf-1',
      kind: 'attribution',
    };
    expect(calibrationActionable(attribution)).toBe(false);
    expect(contestableItem(attribution)).toBe(true);
  });
});
