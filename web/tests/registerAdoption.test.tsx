import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

// HOME'S SLOT CARDS JOIN THE VIEWSCREEN REGISTER — the operator's "this main
// view still has white blocks".
//
// Home was ratified viewscreen and its SHELL went dark, but the cards inside it
// never adopted: the T1/T2/T3 strip, DUTY/RHYTHM/FUEL and the push card were
// still cream panels with green ink sitting on a dark hull.
//
// WHY THESE THREE AND NOT EVERY WARM PANEL IN THE APP. A component may be warm
// legitimately — `pages/share.tsx` renders `<Layout showNav={false}>` with no
// surface prop, so it is a WARM route, and it renders `IngestForm` (which
// contains `ProvenancePreview` and both upload pills). Rewriting a straddler's
// classes to console tokens fixes the dark route and breaks the warm one. These
// three are the ones that render on home and NOWHERE else, which is what makes
// direct token adoption correct for them and a marker seam the right answer for
// the straddlers. That reachability claim is asserted below, not assumed — it is
// the whole justification for the treatment.
//
// TWO DIRECTIONS, because "no warm classes" alone is satisfied by a component
// that has stopped rendering anything: the role tokens must still be here. A
// register restyles chrome and never a verdict, so the T1/T2/T3 state dots and
// the danger ink survive the re-hull unchanged.

const ROOT = join(__dirname, '..');
const ADOPTED = [
  'components/feed/SlotBoard.tsx',
  'components/feed/RingsHeader.tsx',
  'components/PushToggle.tsx',
] as const;

const WARM = /\b(?:bg-white|bg-cream|text-cream|(?:bg|border|text|ring|placeholder|divide|from|to|via)-honeydew-\d+)\b/g;
// Role-carrying classes. These are verdict colour and are inviolate.
const ROLE = /\b(?:bg-status-[a-z-]+|text-status-[a-z-]+|text-danger|bg-danger-bg|text-affirm|bg-affirm[a-z-]*|text-caution|text-negative)\b/g;

function src(rel: string): string {
  return readFileSync(join(ROOT, rel), 'utf8');
}

describe('home adopts the viewscreen register', () => {
  it.each(ADOPTED)('%s carries no warm class', (rel) => {
    expect([...src(rel).matchAll(WARM)].map((m) => m[0])).toEqual([]);
  });

  it('the scanner can still SEE a warm class — the positive control', () => {
    // Without this, a broken pattern would report every file clean and the pins
    // above would be green against a build that never adopted anything.
    const sample = 'className="rounded-xl border border-honeydew-200 bg-cream text-honeydew-700"';
    expect([...sample.matchAll(WARM)].map((m) => m[0])).toEqual([
      'border-honeydew-200',
      'bg-cream',
      'text-honeydew-700',
    ]);
  });

  it('carries the SAME role colours it carried before the re-hull', () => {
    // The half that makes "no warm classes" mean adoption rather than deletion —
    // and it is a per-token census, not a count, because a re-hull that swapped
    // one verdict hue for another would keep the count and break the vocabulary.
    //
    // NOT `.length > 0` per file, which is what this pin said first and which
    // FAILED honestly: PushToggle has no role-coloured element at all (its "Turn
    // off" border was warm CHROME, not a verdict), so a per-file lower bound was
    // asserting something untrue about a correct component.
    //
    // The census below is the census measured across the three files immediately
    // BEFORE the token mapping was applied, via:
    //   grep -ohE "bg-status-[a-z-]+|text-status-[a-z-]+|text-danger|bg-danger-bg" \
    //     components/feed/SlotBoard.tsx components/feed/RingsHeader.tsx \
    //     components/PushToggle.tsx | sort | uniq -c
    const census: Record<string, number> = {};
    for (const rel of ADOPTED) {
      for (const m of src(rel).matchAll(ROLE)) census[m[0]] = (census[m[0]] ?? 0) + 1;
    }
    expect(census['bg-danger-bg']).toBe(1);
    expect(census['bg-status-done-fg']).toBe(2);
    expect(census['bg-status-progress-fg']).toBe(2);
    expect(census['text-danger']).toBe(3);
    expect(census['text-status-done-fg']).toBe(4);
  });

  it('spends no console token at an alpha it cannot honour', () => {
    // `--console-*` are plain hex, NOT the `<alpha-value>` channel form Tailwind
    // needs, so `bg-console-panel/70` compiles to nothing and the element loses
    // its background silently. The tailwind config says so; this catches the
    // translation that forgets it (the re-hull did, once, on the undo bar —
    // `bg-cream/70` became `bg-console-panel/70` before it became the deck's own
    // `bg-affirm-deep`).
    for (const rel of ADOPTED) {
      expect([...src(rel).matchAll(/\bconsole-[a-z-]+\/\d+/g)].map((m) => m[0])).toEqual([]);
    }
  });

  it('re-hulls only components no WARM route renders', () => {
    // The reachability claim this treatment rests on. `/share` takes no surface
    // prop and so is warm; it must not reach any of these three, or the adoption
    // above would be shipping a dark panel onto a warm page.
    const share = src('pages/share.tsx');
    for (const name of ['SlotBoard', 'RingsHeader', 'PushToggle']) {
      expect(share).not.toContain(`<${name}`);
    }
    // POSITIVE CONTROL: /share really is warm and really does render page
    // content — so "does not contain" is a fact about this page, not about an
    // empty file or a mis-read path.
    expect(share).toContain('<Layout showNav={false}>');
    expect(share).toContain('<IngestForm');
    expect(share).not.toContain('surface=');
  });
});
