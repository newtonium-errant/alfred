import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FencedText } from '../components/markdown/FencedText';
import { EvidenceBody } from '../components/feed/EvidenceBody';
import { MessageBubble } from '../components/chat/MessageBubble';
import { BriefView } from '../components/brief/BriefView';

// FENCEDTEXT JOINS THE MARKER SEAM.
//
// One warm literal served four consumers across four dark registers: a fenced
// block rendered as a bright cream slab on every one of them. The chat-bubbles
// adoption is what made it loud — once the hull went dark the block stayed
// light, which is the contrast the operator photographed in the brief's weather.
//
// TWO MARKERS, and `ui-code` rather than `ui-panel` for a mechanical reason:
// `ui-panel`'s ink rules are DESCENDANT selectors, and a fence carries its ink
// on the same element as its background (the `<pre>`), so `ui-panel` would light
// the slab and leave dark text sitting on it.
//
// REACHABILITY IS MEASURED HERE, NOT ASSUMED — in both directions. Every
// register that claims a rule has a member driven through its real component
// below, and crt is asserted to have NO rule because no consumer reaches it.

const WEB = join(__dirname, '..');
const sheet = (f: string) => readFileSync(join(WEB, 'styles', f), 'utf8');

const FENCE = 'Here it is:\n\n```csv\na,b\n1,2\n```\n';

/** The four registers that can actually render a fence, and their sheets. */
const REGISTERS: [name: string, surface: string, file: string, prefix: string][] = [
  ['comms (chat)', 'comms', 'comms.css', '--comms-'],
  ['viewscreen (player)', 'viewscreen', 'viewscreen.css', '--viewscreen-'],
  ['sensor-log (feed)', 'sensor-log', 'sensorLog.css', '--sensor-'],
  ['console (deck)', 'console', 'console.css', '--console-'],
];

describe('the block wears the marker', () => {
  it('the fenced block and its label both carry one', () => {
    render(<FencedText text={FENCE} nameHint="t" />);
    expect(screen.getByTestId('fenced-block-content').classList.contains('ui-code')).toBe(true);
    expect(screen.getByTestId('fenced-block-lang').classList.contains('ui-code-label')).toBe(true);
  });

  it('the warm literals STAY as the unmarked default', () => {
    // The marker reaches in; it does not replace the chrome. This is what keeps
    // a future warm consumer rendering as it does today rather than shipping a
    // dark slab onto a light page — the /share lesson, applied before there is
    // a /share to be bitten by. (There is no warm consumer today: all four
    // terminate on marked dark surfaces.)
    render(<FencedText text={FENCE} nameHint="t" />);
    const cls = screen.getByTestId('fenced-block-content').className;
    expect(cls).toContain('bg-honeydew-100');
    expect(cls).toContain('border-honeydew-300');
  });

  it('the Download button rides the EXISTING button seam', () => {
    // It needs no new vocabulary — `Button` already emits `.ui-btn`, which every
    // register claims. Asserted rather than assumed, because "presumably kit" is
    // how an unadopted control ships.
    render(<FencedText text={FENCE} nameHint="t" />);
    expect(screen.getByTestId('fenced-block-download').classList.contains('ui-btn')).toBe(true);
  });
});

describe('every register that claims the marker can actually reach it', () => {
  it.each(REGISTERS)('%s defines both rules, scoped, from its own ramp', (_n, surface, file, prefix) => {
    const css = sheet(file);
    for (const cls of ['ui-code', 'ui-code-label']) {
      const rule = css
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('}')
        .map((c) => ({ sel: c.slice(0, c.indexOf('{')).trim(), body: c.slice(c.indexOf('{') + 1) }))
        .find((r) => new RegExp(`\\.${cls}\\s*$`).test(r.sel));
      expect(rule, `${file} has no .${cls} rule`).toBeTruthy();
      // Scoped to its surface, so warm stays the unmarked default…
      expect(rule!.sel).toContain(`[data-surface='${surface}']`);
      // …and painted from that register's OWN vocabulary, never a literal.
      expect(rule!.body).toContain(`var(${prefix}`);
      expect(rule!.body).not.toMatch(/#[0-9a-fA-F]{3,8}/);
    }
  });

  it('the block rule spends RAISE — the depth a fence sits at', () => {
    // Vacuity control on the values: four rules that all resolved to `panel`
    // would satisfy the "uses a var" assertion while flattening the fence into
    // the card it sits inside.
    for (const [, , file, prefix] of REGISTERS) {
      expect(sheet(file)).toContain(`background-color: var(${prefix}raise)`);
    }
  });
});

describe('reachability, driven through the real components', () => {
  // The tiebreaker the seam is built on: a register claims a rule when a member
  // can reach it. These render a fence through each consumer rather than
  // reasoning about the call graph.

  it('comms — an assistant turn renders a fence', () => {
    render(<MessageBubble role="assistant" text={FENCE} ts="2026-08-15T09:00:00Z" />);
    expect(screen.getByTestId('fenced-block-content').classList.contains('ui-code')).toBe(true);
  });

  it('viewscreen — the brief document renders a fence', () => {
    render(
      <BriefView testId="brief-view" title="Morning Brief" date="2026-08-15" markdown={FENCE} emptyMessage="none" />,
    );
    expect(screen.getByTestId('fenced-block-content').classList.contains('ui-code')).toBe(true);
  });

  it.each([
    ['sensor-log (FeedRow default)', undefined],
    ['console (DeckCard passes surface)', 'console' as const],
  ])('%s — an evidence body renders a fence', (_label, surface) => {
    // THE ONE THE LANE WAS TOLD TO VERIFY RATHER THAN ASSUME. EvidenceBody is
    // the shared consumer behind BOTH the feed row and the deck card, which is
    // why this is four registers and not three.
    //
    // The evidence shape is `{ body }` — `evidenceBody` reads `ev.body` and
    // returns null for anything else, and EvidenceBody renders NOTHING when it
    // gets null. Worth stating because the first draft of this test used
    // `{ text }` and went red: read carelessly, that red says "sensor-log and
    // console cannot reach a fence, drop their rules" — which would have
    // deleted two correct rules on the strength of a typo in the fixture.
    render(
      <EvidenceBody
        evidence={{ body: FENCE } as never}
        {...(surface ? { surface } : {})}
      />,
    );
    expect(screen.getByTestId('fenced-block-content').classList.contains('ui-code')).toBe(true);
  });
});

describe('the deck really does pass its surface', () => {
  it('DeckCard renders EvidenceBody with the CONSOLE skin, not the warm default', async () => {
    // NOTE-2, closed. The reachability pin above is named "console (DeckCard
    // passes surface)" and proved only that `EvidenceBody` honours a surface it
    // is HANDED — it passed `surface="console"` itself. Nothing checked the
    // claim in its own name, so the day DeckCard stopped passing the prop, the
    // deck's evidence would silently fall back to the warm skin (honeydew on a
    // near-black card) and that pin would stay green.
    //
    // DRIVEN, NOT GREPPED. A source-assert for `surface="console"` at the call
    // site is what this file family has been burned by three times: the comment
    // above a marker contains the marker's name, so `toContain` passes on prose.
    // `skinFor` returns different CLASS STRINGS per surface, so the DOM answers
    // the question directly.
    const { DeckCard } = await import('../components/feed/DeckCard');
    const { withServedActions } = await import('./helpers/servedActions');
    render(
      <DeckCard
        item={
          withServedActions({
            id: 'email_tier:note/A.md',
            kind: 'email_tier',
            instance: 'salem',
            title: 'Email tier: a@b.com — Subject',
            mode: 'decide',
            attention: 'needs_you',
            evidence: { body: FENCE },
            actions: [],
            state: 'open',
            created_at: '2026-07-30T00:00:00Z',
            acted_at: null,
            expires_at: null,
            source_ref: {},
          }) as never
        }
        depth={0}
        expanded
        confirming={false}
        onToggleEvidence={() => {}}
        onConfirmHeavy={() => {}}
        onCancelHeavy={() => {}}
      />,
    );

    const frame = screen.getByTestId('evidence-body').firstElementChild as HTMLElement;
    // The console skin's frame, and NOT the warm one — both directions, because
    // asserting only the presence would pass if the element carried both.
    expect(frame.className).toContain('bg-console-void');
    expect(frame.className).not.toContain('bg-honeydew-50');
  });
});

describe('the registers that must NOT claim it', () => {
  it('crt defines no ui-code rule — no consumer reaches a crt surface', () => {
    // The tiebreaker cutting the other way, and the reason this is a pin rather
    // than a comment: crt already claims `ui-panel`, so adding `ui-code` there
    // for symmetry would look tidy and ship a rule nothing can render. If a
    // FencedText consumer ever lands on batch / ingest / login, this fails and
    // the rule gets added deliberately.
    expect(sheet('crt.css')).toContain('.ui-panel'); // positive control: the file IS a marker sheet
    // DELIBERATELY `includes`, NOT `exactToken` — the one site in this sweep
    // that keeps the substring match, because here the substring makes the
    // assertion STRICTER rather than weaker. This is an ABSENCE check: matching
    // loosely means `.ui-code-label`, `.ui-code-anything` and `.ui-code` itself
    // all count as the rule appearing, so the assertion fails on any of them.
    // Tightening it to an exact token would NARROW an absence check — crt could
    // then grow a `.ui-code-label` rule and this would still pass.
    //
    // The general form, since it is the part worth carrying: exact-token is
    // right for PRESENCE and reach ("is this specific rule here / does this
    // selector target that class"), and wrong for ABSENCE of a family ("has
    // anything from this family appeared"). Direction of the assertion decides,
    // not the shape of the string.
    expect(sheet('crt.css').includes('.ui-code')).toBe(false);
  });
});
