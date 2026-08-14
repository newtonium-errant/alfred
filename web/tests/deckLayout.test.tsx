import { createRequire } from 'node:module';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// THE DECK'S LAYOUT GUARANTEES, from an operator bug report: "the deck cards
// overlap the buttons beneath them."
//
// The report reads like a tall-card bug and is not one. A deck card is
// `absolute inset-0 m-auto max-h-[380px]` inside its stack box, so its height is
// IDENTICAL expanded and collapsed, and the details region has carried
// `min-h-0 flex-1 overflow-y-auto` since an earlier lane. What actually reaches
// the buttons is the STACK: each card behind the top is translated DOWN
// `DECK_CARD_DEPTH_OFFSET_PX` per depth, so the deepest one hangs below the box
// that contains it — onto a button row whose only separation was its own 4px of
// padding.
//
// jsdom has no layout engine, so none of this can be pinned in pixels. It CAN be
// pinned as a relationship: the reservation and the transform that makes it
// necessary must read the same constants, and the buttons must live outside the
// box the cards are drawn in. A pin that measured offsets would be measuring
// jsdom's zeros; these measure the thing that was actually wrong.

const { mockList } = vi.hoisted(() => ({ mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import { Deck, DECK_COLUMN_MIN_PX, DECK_STACK_RESERVE_PX } from '../components/feed/Deck';
import {
  DeckCard,
  DECK_CARD_DEPTH_OFFSET_PX,
  DECK_CARD_SHADOW_BLUR_PX,
  DECK_CARD_SHADOW_CLASS,
  DECK_CARD_SHADOW_REACH_PX,
  DECK_CARD_SHADOW_Y_PX,
  DECK_MAX_VISIBLE_DEPTH,
} from '../components/feed/DeckCard';
import { FILE_PILL_CLASS } from '../components/ui/button';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import DeckPage from '../pages/deck';
import { evidenceList } from '../lib/algernon/feedEvidence';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// The REAL Tailwind theme, resolved from the project's own config — the only way
// to tell a class that emits CSS from one that silently emits nothing.
const req = createRequire(import.meta.url);
const TAILWIND_SPACING = req('tailwindcss/resolveConfig')(req('../tailwind.config.cjs')).theme
  .spacing as Record<string, string>;

function item(id: string, evidence: Record<string, unknown> = { sender: 'a@b.com' }): FeedItem {
  return withServedActions({
    id,
    kind: 'email_tier',
    instance: 'salem',
    title: `Email tier: ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence,
    actions: [],
    state: 'open',
    created_at: '2026-08-14T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

function renderCard(evidence: Record<string, unknown>) {
  return render(
    <DeckCard
      item={item('a', evidence)}
      depth={0}
      expanded
      confirming={false}
      onToggleEvidence={() => {}}
      onConfirmHeavy={() => {}}
      onCancelHeavy={() => {}}
    />,
  );
}

afterEach(() => {
  mockList.mockReset();
  vi.restoreAllMocks();
});

describe('the card stack cannot reach the verb buttons', () => {
  it('clears BOTH footprints — the deepest card AND the shadow it casts', () => {
    // Two terms, because the card occupies space two ways. `>=` on either alone
    // would be satisfied by a zero gap, which is the state the operator
    // photographed: edges touching under a shadow lying across the buttons.
    const spill = DECK_CARD_DEPTH_OFFSET_PX * DECK_MAX_VISIBLE_DEPTH;
    expect(DECK_STACK_RESERVE_PX).toBeGreaterThan(spill + DECK_CARD_SHADOW_REACH_PX);
    // And the shadow term is not a rounding error next to the geometric one —
    // it is the larger of the two, which is why counting only geometry left the
    // complaint standing.
    expect(DECK_CARD_SHADOW_REACH_PX).toBeGreaterThan(spill);
  });

  it('takes its shadow arithmetic from the class that actually renders', () => {
    // The class is a Tailwind ARBITRARY value and must stay written out — the JIT
    // scans source text, so a class name built from these constants would emit no
    // CSS at all (the `h-13` species again, one file over). That leaves two
    // sources of truth, so this reconciles them: the literal is what paints, the
    // constants are what the reserve is computed from, and they must agree.
    const m = /shadow-\[0_(\d+)px_(\d+)px_/.exec(DECK_CARD_SHADOW_CLASS);
    expect(m).not.toBeNull(); // positive control: the literal is still parseable
    expect(Number(m?.[1])).toBe(DECK_CARD_SHADOW_Y_PX);
    expect(Number(m?.[2])).toBe(DECK_CARD_SHADOW_BLUR_PX);
    // A drop shadow reaches its offset plus half its blur past the edge.
    expect(DECK_CARD_SHADOW_REACH_PX).toBe(DECK_CARD_SHADOW_Y_PX + DECK_CARD_SHADOW_BLUR_PX / 2);
    // ...and the literal is the one the card is actually wearing.
    render(<Deck items={[item('a')]} />);
    expect(screen.getAllByTestId('deck-card')[0].className).toContain(DECK_CARD_SHADOW_CLASS);
  });

  it('declares that reservation on the stack box, and the deepest card spills by the SAME constant', () => {
    render(<Deck items={[item('a'), item('b'), item('c')]} />);

    const cards = screen.getAllByTestId('deck-card');
    // POSITIVE CONTROL. With fewer than three cards there is no depth-2 card and
    // the transform assertion below would pass by having nothing to check.
    expect(cards.length).toBe(DECK_MAX_VISIBLE_DEPTH + 1);

    const stack = screen.getByTestId('deck-stack');
    expect(stack.style.marginBottom).toBe(`${DECK_STACK_RESERVE_PX}px`);

    // The other end of the same number: the reservation is only correct if the
    // transform it was derived from still spills by that much. Pinning both to
    // the constant is what stops one moving without the other.
    const deepest = cards[cards.length - 1];
    expect(deepest.style.transform).toContain(
      `translateY(${DECK_MAX_VISIBLE_DEPTH * DECK_CARD_DEPTH_OFFSET_PX}px)`,
    );
  });

  it('keeps the buttons OUTSIDE the box the cards are drawn in', () => {
    render(<Deck items={[item('a'), item('b'), item('c')]} />);
    const stack = screen.getByTestId('deck-stack');

    // POSITIVE CONTROL for `contains` — a broken query would report "not inside"
    // for everything, including the cards that genuinely are.
    expect(stack.contains(screen.getAllByTestId('deck-card')[0])).toBe(true);

    for (const id of ['deck-btn-reject', 'deck-btn-snooze', 'deck-btn-details', 'deck-btn-affirm']) {
      expect(stack.contains(screen.getByTestId(id))).toBe(false);
    }
  });

  it('every spacing utility on the gesture buttons is one Tailwind actually emits', () => {
    // `h-13 w-13` compiled to NOTHING for as long as it was there: Tailwind 3.4's
    // default scale goes 12 → 14 and this project extends no spacing. The buttons
    // were therefore ~40px (padding + glyph), under the 44pt touch minimum — the
    // other half of why that row read as crowded. A dead class is invisible to
    // every DOM assertion, so this checks the class against the real theme.
    render(<Deck items={[item('a')]} />);
    const cls = screen.getByTestId('deck-btn-affirm').className;

    const sized = [...cls.matchAll(/(?:^|\s)([hw])-(\[[^\]]+\]|[\w.]+)/g)];
    expect(sized.map((m) => m[1]).sort()).toEqual(['h', 'w']); // both axes declared

    for (const match of sized) {
      const value = match[2];
      if (value.startsWith('[')) continue; // arbitrary value — emitted by construction
      // hasOwnProperty, not toHaveProperty: a scale step like `0.5` would be read
      // as a nested path by the matcher and pass against a theme without it.
      expect(Object.prototype.hasOwnProperty.call(TAILWIND_SPACING, value)).toBe(true);
    }
  });

  it('the PAGE takes its floor from the deck, so the clearance is added and not absorbed', async () => {
    // The wrapper is a MIN-height. Left at its old literal, the reservation would
    // have come out of the card's own height instead of out of the page — the card
    // shrinking by exactly the gap the buttons gained.
    mockList.mockResolvedValue({ items: [item('a'), item('b'), item('c')] });
    render(<DeckPage />);

    await waitFor(() => expect(screen.getByTestId('deck')).not.toBeNull());
    expect(screen.getByTestId('deck').parentElement?.style.minHeight).toBe(`${DECK_COLUMN_MIN_PX}px`);
    expect(DECK_COLUMN_MIN_PX).toBeGreaterThan(DECK_STACK_RESERVE_PX);
  });
});

// Walk every .tsx under the app's source roots — the sweep has to be able to see
// a file nobody thought to list, which is the whole point of a species claim.
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== 'node_modules' && e.name !== '.next') sourceFiles(p, out);
    } else if (e.name.endsWith('.tsx')) out.push(p);
  }
  return out;
}

// A <label> carrying a LITERAL className that dresses it as a pill. This is the
// shape of the defect: `className="…rounded-xl border border-honeydew-300
// bg-white…"` typed by hand instead of taken from the kit, so `ui-btn` never
// arrives and the register cannot reach it.
const BESPOKE_PILL_LABEL = /<label[^>]*className="([^"]*(?:bg-white|bg-honeydew-)[^"]*rounded[^"]*|[^"]*rounded[^"]*(?:bg-white|bg-honeydew-)[^"]*)"/g;

describe('bespoke file-picker pills — the species, not the five instances', () => {
  const ROOTS = [join(__dirname, '..', 'components'), join(__dirname, '..', 'pages')];

  it('routes every file-picker label through the kit, so the marker arrives by construction', () => {
    expect(FILE_PILL_CLASS).toContain('ui-btn');
    // The kit's own marker, not a string this constant happens to contain: the
    // pill IS the outline button plus a cursor, so it must also carry that
    // variant's chrome.
    expect(FILE_PILL_CLASS).toContain('cursor-pointer');
  });

  it('leaves NO hand-dressed <label> anywhere in components/ or pages/', () => {
    const offenders: string[] = [];
    let scanned = 0;
    for (const root of ROOTS) {
      for (const file of sourceFiles(root)) {
        scanned += 1;
        const src = readFileSync(file, 'utf8');
        for (const m of src.matchAll(BESPOKE_PILL_LABEL)) {
          offenders.push(`${file.replace(join(__dirname, '..'), '.')}: ${m[1].slice(0, 60)}…`);
        }
      }
    }
    // POSITIVE CONTROLS, both halves. The sweep reached real files...
    expect(scanned).toBeGreaterThan(30);
    // ...and the matcher can actually see the defect it claims is extinct. Without
    // this the assertion below would pass just as happily against a broken regex.
    const known = `<label className="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-honeydew-300 bg-white px-3 py-1.5">`;
    expect([...known.matchAll(BESPOKE_PILL_LABEL)].length).toBe(1);

    expect(offenders).toEqual([]);
  });
});

describe('the details region — chrome outside the scroller', () => {
  it('scrolls, and its divider sits on a box that does NOT move', () => {
    renderCard({ sender: 'a@b.com', snippet: 'x'.repeat(600) });
    const evidence = screen.getByTestId('deck-evidence');

    // Containment, unchanged from the earlier lane and still load-bearing.
    expect(evidence.className).toContain('overflow-y-auto');
    expect(evidence.className).toContain('min-h-0');

    // The defect: the rule used to be a `border-t` on the SCROLLING element, so
    // content scrolled up under the padding and the divider drew straight through
    // whichever row straddled the boundary — a half-cut line of caps with a line
    // across it, which is what the operator read as two text layers.
    expect(evidence.className).not.toContain('border-t');

    // ...and the divider still EXISTS. Without this half, deleting the border
    // outright would satisfy the assertion above just as well as fixing it does.
    const chrome = evidence.parentElement;
    expect(chrome?.className).toContain('border-t');
    expect(chrome?.className).not.toContain('overflow-y-auto');
  });

  it('gives each row its label on its own line, so a long value gets the full card width', () => {
    // A `shrink-0` label ate ~157px of a ~330px card, leaving long values a narrow
    // column that ran a dozen wrapped lines down the side of their own label.
    renderCard({ cluster_record_paths: ['note/A.md'] });

    const label = screen.getByText('Cluster Record Paths');
    expect(label.tagName).toBe('DT');
    expect(label.className).not.toContain('shrink-0');

    const row = label.parentElement;
    expect(row?.className ?? '').not.toContain('flex'); // not a side-by-side row
    // POSITIVE CONTROL: the value really is in that row, so "no flex" is a claim
    // about the arrangement rather than about a row that isn't there.
    expect(row?.querySelector('dd')?.textContent).toContain('note/A');
  });
});

describe('evidenceList — an array of strings is a list, not a JSON blob', () => {
  it('splits a vault record path into its name and its directory', () => {
    const list = evidenceList(['note/Cineplex Account Locked 2026-06-12.md']);
    expect(list?.entries).toEqual([{ name: 'Cineplex Account Locked 2026-06-12', prefix: 'note/' }]);
    expect(list?.total).toBe(1);
  });

  it('degrades to the whole string when an entry is not a path', () => {
    expect(evidenceList(['kalle'])?.entries).toEqual([{ name: 'kalle', prefix: '' }]);
    // A trailing slash would otherwise leave a blank name — a line that shows nothing.
    expect(evidenceList(['note/'])?.entries).toEqual([{ name: 'note/', prefix: '' }]);
  });

  it('caps the entries but REPORTS the true total', () => {
    const list = evidenceList(Array.from({ length: 12 }, (_, i) => `note/R${i}.md`));
    expect(list?.entries.length).toBe(8);
    expect(list?.total).toBe(12); // the card says "+4 more" from this
  });

  it('refuses anything that is not a non-empty array of non-empty strings', () => {
    expect(evidenceList([])).toBeNull();
    expect(evidenceList([1, 2])).toBeNull(); // already legible as JSON at that size
    expect(evidenceList(['ok', 3])).toBeNull(); // mixed
    expect(evidenceList(['ok', '  '])).toBeNull(); // blank entry
    expect(evidenceList('note/A.md')).toBeNull();
    expect(evidenceList(null)).toBeNull();
    // POSITIVE CONTROL — the nearest admissible neighbour of every refusal above.
    expect(evidenceList(['ok', 'fine'])?.entries.length).toBe(2);
  });
});

describe('the card draws record paths as a list', () => {
  it('renders one name per line with no JSON syntax', () => {
    renderCard({
      cluster_record_paths: [
        'note/Cineplex Account Locked 2026-06-12.md',
        'note/Cineplex Account Locked 2026-06-01.md',
      ],
    });
    const list = screen.getByTestId('deck-evidence-list');
    expect(list.querySelectorAll('li').length).toBe(2);
    expect(list.textContent).toContain('Cineplex Account Locked 2026-06-12');
    // The whole point: no brackets, no quotes, no `.md` tails.
    expect(list.textContent).not.toContain('[');
    expect(list.textContent).not.toContain('"');
    expect(list.textContent).not.toContain('.md');
    expect(screen.queryByTestId('deck-evidence-list-more')).toBeNull();
  });

  it('says how many it did not draw rather than dropping them silently', () => {
    renderCard({ cluster_record_paths: Array.from({ length: 12 }, (_, i) => `note/R${i}.md`) });
    // 8 entries + the cue, which is itself an <li> in the same list.
    expect(screen.getByTestId('deck-evidence-list').querySelectorAll('li').length).toBe(9);
    expect(screen.getByTestId('deck-evidence-list-more').textContent).toContain('+4 more');
  });

  it('leaves a scalar value exactly as it was — the list is conditional, not the row', () => {
    // Without this the list pins would pass just as happily against a build whose
    // evidence rows had stopped rendering anything at all.
    renderCard({ sender: 'a@b.com' });
    expect(screen.queryByTestId('deck-evidence-list')).toBeNull();
    expect(screen.getByTestId('deck-evidence').textContent).toContain('a@b.com');
  });
});
