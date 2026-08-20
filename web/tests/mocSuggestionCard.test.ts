// The MOC reader's client half (2026-08-20) — the hold primitive's OPEN-SET
// arm, and the coupling that makes a quiet card reachable at all.
//
// The lane's central finding, pinned here because it is invisible in Python:
// a (fyi, fyi) card reaches the deck ONLY through `isDeckCandidate`'s
// `hasSuggestedChoice` branch, so a MOC card without >= 2 co-equal choices
// would be served an apply verb and rendered on no surface that offers one.
// The FYI glance row gets ack/contest and NO verbs.
import { describe, it, expect } from 'vitest';
import {
  holdChoicesFor,
  holdChoicesForVerb,
  hasSuggestedChoice,
  isDeckCandidate,
  isDeckDealt,
  verbsFromActions,
} from '../lib/algernon/feedConstants';
import { isNeedsYouItem } from '../lib/algernon/feedNeedsYou';
import served from './helpers/servedActions.json';

const MOC_VERBS = (served as { kinds: Record<string, unknown[]> }).kinds.moc_suggestion;

function mocCard(choices: unknown[] | undefined, overrides: Record<string, unknown> = {}) {
  return {
    id: 'feed:moc_suggestion:ms-1',
    kind: 'moc_suggestion',
    mode: 'fyi',
    attention: 'fyi',
    title: 'Add 3 notes to Roman Philosophy MOC?',
    actions: MOC_VERBS,
    evidence: {
      suggestion_id: 'ms-1',
      proposed_target: 'MOC/Roman Philosophy MOC.md',
      moc_choices: choices,
      ...overrides,
    },
  } as never;
}

const TWO_CHOICES = [
  { target: 'MOC/Roman Philosophy MOC.md', label: 'Roman Philosophy MOC', suggested: true },
  { target: 'MOC/Stoicism MOC.md', label: 'Stoicism MOC', suggested: false },
];

describe('the card is quiet, and quiet is load-bearing', () => {
  it('never rings the phone — the served pair is (fyi, fyi)', () => {
    const item = mocCard(TWO_CHOICES);
    // THE PAIR, read back off the item rather than off the fixture literal.
    // `isNeedsYouItem` is what the push poller fetches on, with no kind
    // allowlist, so decide-mode would ring REGARDLESS of attention.
    const { mode, attention } = item as unknown as {
      mode: string;
      attention: string;
    };
    expect(mode).toBe('fyi');
    expect(attention).toBe('fyi');
    expect(isNeedsYouItem(item)).toBe(false);
  });

  it('POSITIVE CONTROL: flipping only the mode to decide WOULD ring', () => {
    // Without this the pin above passes against a build where nothing rings.
    const ringing = { ...(mocCard(TWO_CHOICES) as object), mode: 'decide' } as never;
    expect(isNeedsYouItem(ringing)).toBe(true);
  });

  it('reaches the deck through hasSuggestedChoice, not through decide-mode', () => {
    const item = mocCard(TWO_CHOICES);
    expect(hasSuggestedChoice(item)).toBe(true);
    expect(isDeckCandidate(item)).toBe(true);
    expect(isDeckDealt(item)).toBe(true);
  });

  it('WITHOUT a choice family the quiet card is NOT a deck candidate', () => {
    // The reason the producer refuses to emit such a row. Note isDeckDealt
    // still answers true — the card has verbs — which is exactly why the
    // shortfall is invisible without this pin: it looks dealable and is
    // never fetched.
    const item = mocCard(undefined);
    expect(hasSuggestedChoice(item)).toBe(false);
    expect(isDeckCandidate(item)).toBe(false);
    expect(isDeckDealt(item)).toBe(true);
  });

  it('a family of ONE is no family (the pattern forbids a selector of one)', () => {
    const item = mocCard([TWO_CHOICES[0]]);
    expect(holdChoicesFor(item, 'affirm')).toBeNull();
    expect(isDeckCandidate(item)).toBe(false);
  });
});

describe('the open-set arm', () => {
  it('derives the family from evidence, all sharing the ONE served verb', () => {
    const choices = holdChoicesFor(mocCard(TWO_CHOICES), 'affirm');
    expect(choices).not.toBeNull();
    expect(choices).toHaveLength(2);
    // ONE verb, distinguished by target — the whole point of the arm.
    expect(new Set(choices!.map((c) => c.verb))).toEqual(new Set(['moc_apply']));
    expect(choices!.map((c) => c.target)).toEqual([
      'MOC/Roman Philosophy MOC.md',
      'MOC/Stoicism MOC.md',
    ]);
  });

  it('marks the proposed target as the suggested member', () => {
    const choices = holdChoicesFor(mocCard(TWO_CHOICES), 'affirm')!;
    const suggested = choices.filter((c) => c.suggested);
    expect(suggested).toHaveLength(1);
    expect(suggested[0].target).toBe('MOC/Roman Philosophy MOC.md');
  });

  it('drops malformed choices rather than coercing them', () => {
    const choices = holdChoicesFor(
      mocCard([
        ...TWO_CHOICES,
        { label: 'no target' },
        null,
        'a string',
        { target: '', label: 'empty' },
      ]),
      'affirm',
    )!;
    expect(choices).toHaveLength(2);
  });

  it('falls back to the target as the label when the label is missing', () => {
    const choices = holdChoicesFor(
      mocCard([
        { target: 'MOC/A.md', suggested: true },
        { target: 'MOC/B.md', suggested: false },
      ]),
      'affirm',
    )!;
    expect(choices.map((c) => c.label)).toEqual(['MOC/A.md', 'MOC/B.md']);
  });

  it('serves exactly one affirm verb, so the ceiling stays a static map', () => {
    const verbs = verbsFromActions(mocCard(TWO_CHOICES));
    expect(verbs!.affirm).toBe('moc_apply');
    // The reject gesture is the quick defer — "not now", never a decline.
    expect(verbs!.reject).toBe('defer');
  });
});

describe('the STATIC arm is untouched — the two shipped consumers', () => {
  // Constraint (a) of the 2026-08-20 ruling, driven rather than asserted:
  // sort and the when-family must derive exactly as they did before the
  // dynamic arm existed. Both are built from the SERVED fixture, so a change
  // to their ceiling entries would surface here too.
  const sortVerbs = (served as { kinds: Record<string, unknown[]> }).kinds.sort_suggestion;

  function sortCard() {
    return {
      id: 'feed:sort_suggestion:s-1',
      kind: 'sort_suggestion',
      mode: 'fyi',
      attention: 'fyi',
      title: 'Sort: something',
      actions: (sortVerbs as Array<Record<string, unknown>>).map((a) =>
        a.verb === 'sort_duty' ? { ...a, gesture: 'affirm' } : a,
      ),
      evidence: { proposed_slot: 'duty' },
    } as never;
  }

  it('sort still derives a THREE-verb static family with no targets', () => {
    const choices = holdChoicesFor(sortCard(), 'affirm')!;
    expect(choices).toHaveLength(3);
    expect(choices.map((c) => c.verb)).toEqual(['sort_duty', 'sort_rhythm', 'sort_fuel']);
    // The static arm carries NO target — the verb is the whole choice.
    expect(choices.every((c) => c.target === undefined)).toBe(true);
  });

  it('a static family is NOT diverted by an evidence key it does not use', () => {
    // The dynamic arm reads `evidence.moc_choices` by NAME. A sort card that
    // somehow carried one must still resolve through its static family —
    // otherwise the new arm could hijack an existing consumer.
    const card = sortCard() as unknown as { evidence: Record<string, unknown> };
    card.evidence.moc_choices = TWO_CHOICES;
    const choices = holdChoicesForVerb(card as never, 'sort_duty')!;
    expect(choices).toHaveLength(3);
    expect(choices.every((c) => c.target === undefined)).toBe(true);
  });
});
