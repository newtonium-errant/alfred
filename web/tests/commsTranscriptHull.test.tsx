import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { hasExactToken } from './_exactToken';
import { MessageBubble } from '../components/chat/MessageBubble';
import { TypingIndicator } from '../components/chat/TypingIndicator';
import {
  COMMS_QUOTED_CLASS,
  COMMS_SURFACE,
  COMMS_TURN_ASSISTANT_CLASS,
  COMMS_TURN_OPERATOR_CLASS,
} from '../lib/algernon/commsSurface';
import type { ChatRole } from '../lib/algernon/types';

// THE TRANSCRIPT'S HULL (comms register).
//
// The quotation adopted the assistant's VOICE and left the panel it is spoken
// from warm — `bg-cream` under monospace phosphor, which is what the operator
// photographed and reported as bright. These pin the hull half.
//
// The hull is deliberately NOT part of the quotation: it reads the surface's
// ordinary depth ramp, never `--comms-quoted-*`. commsQuotation.test.tsx owns
// that boundary and would fail if these rules crossed it, so nothing here
// restates it — what IS pinned here is the complement, that the transcript
// rules are real (they read the depth ramp) and surface-scoped (warm stays the
// unmarked default).

const CSS: string = require('node:fs').readFileSync(
  require('node:path').join(__dirname, '..', 'styles', 'comms.css'),
  'utf8',
);

/** The stylesheet's rules, comments stripped, as selector/body pairs. */
function rules(): { sel: string; body: string }[] {
  return CSS.replace(/\/\*[\s\S]*?\*\//g, '')
    .split('}')
    .map((chunk) => {
      const i = chunk.indexOf('{');
      return i < 0 ? null : { sel: chunk.slice(0, i).trim(), body: chunk.slice(i + 1) };
    })
    .filter((r): r is { sel: string; body: string } => r != null);
}

function bubble(role: ChatRole) {
  render(<MessageBubble role={role} text="a line" ts="2026-08-14T09:00:00Z" />);
  return screen.getByTestId(`msg-${role}`).firstElementChild as HTMLElement;
}

/** Role → the hull class that role's turn must carry. */
const HULL_BY_ROLE: [ChatRole, string][] = [
  ['assistant', COMMS_TURN_ASSISTANT_CLASS],
  ['user', COMMS_TURN_OPERATOR_CLASS],
];

describe('every turn adopts a hull', () => {
  it.each(HULL_BY_ROLE)('a %s turn carries its comms hull', (role, hull) => {
    expect(bubble(role).classList.contains(hull)).toBe(true);
  });

  it.each(HULL_BY_ROLE)(
    'a %s turn keeps the warm utilities as the unmarked default',
    (role) => {
      // The register reaches in through the scoped class; it does not REPLACE
      // the warm chrome. That is what lets a shell which remounts a bubble
      // off-surface render warm rather than a dark panel on a light page — the
      // `ui-panel` lesson, which cost a /share regression to learn.
      const el = bubble(role);
      const warm = role === 'user' ? 'bg-honeydew-500' : 'bg-cream';
      expect(el.classList.contains(warm)).toBe(true);
    },
  );

  it('the two hulls are DIFFERENT — operator and computer stay tellable apart', () => {
    // The distinction is the point: the face carries it (proportional vs the
    // borrowed monospace, pinned next door) and the hull carries it on the
    // depth axis. Flattening both to one class would satisfy "adopted" and lose
    // the conversation's shape.
    const assistantHull = bubble('assistant');
    const operatorHull = bubble('user');
    // Non-empty control FIRST: two elements that both carried nothing would
    // satisfy an inequality assertion while proving nothing at all.
    expect(COMMS_TURN_ASSISTANT_CLASS.length).toBeGreaterThan(0);
    expect(COMMS_TURN_OPERATOR_CLASS.length).toBeGreaterThan(0);
    expect(COMMS_TURN_ASSISTANT_CLASS).not.toBe(COMMS_TURN_OPERATOR_CLASS);
    expect(assistantHull.classList.contains(COMMS_TURN_OPERATOR_CLASS)).toBe(false);
    expect(operatorHull.classList.contains(COMMS_TURN_ASSISTANT_CLASS)).toBe(false);
  });

  it('the in-flight bubble wears the SAME hull as the reply it precedes', () => {
    // The sibling. Adopting the reply and not this one leaves a bright slab
    // flashing between every message and its answer — done at rest, wrong in
    // motion.
    render(<TypingIndicator />);
    const hull = screen.getByTestId('typing-indicator').firstElementChild as HTMLElement;
    expect(hull.classList.contains(COMMS_TURN_ASSISTANT_CLASS)).toBe(true);
  });
});

describe('the hull rules are real, and scoped', () => {
  it.each([COMMS_TURN_ASSISTANT_CLASS, COMMS_TURN_OPERATOR_CLASS])(
    'the stylesheet defines .%s, scoped to the comms surface',
    (hull) => {
      // Markup and stylesheet cannot share a symbol, so this is what keeps the
      // two spellings equal — a hull styled by a class no rule matches is
      // invisible, and every DOM assertion above would still pass.
      const matching = rules().filter((r) => hasExactToken(r.sel, `.${hull}`));
      expect(matching.length).toBeGreaterThan(0);
      for (const rule of matching) {
        expect(rule.sel).toContain(`[data-surface='${COMMS_SURFACE}']`);
      }
    },
  );

  it.each([COMMS_TURN_ASSISTANT_CLASS, COMMS_TURN_OPERATOR_CLASS])(
    '.%s paints from the surface depth ramp, not from a literal',
    (hull) => {
      // An adopted hull whose background was a hex literal would look right
      // today and drift the moment the ramp moves. It must spend a token.
      const rule = rules().find((r) => hasExactToken(r.sel, `.${hull}`));
      expect(rule).toBeTruthy();
      expect(rule!.body).toMatch(/background-color:\s*var\(--comms-/);
      expect(rule!.body).not.toMatch(/#[0-9a-fA-F]{3,8}/);
    },
  );

  it('the two hulls sit at DIFFERENT depths in the ramp', () => {
    // Same reasoning as the class-inequality pin, one layer down: two classes
    // that both resolved to `--comms-panel` would be distinct names for one
    // appearance, which is the flattening this is meant to prevent.
    const bg = (hull: string) =>
      rules()
        .find((r) => hasExactToken(r.sel, `.${hull}`))
        ?.body.match(/background-color:\s*(var\([^)]*\))/)?.[1];
    const assistantBg = bg(COMMS_TURN_ASSISTANT_CLASS);
    const operatorBg = bg(COMMS_TURN_OPERATOR_CLASS);
    expect(assistantBg).toBeTruthy();
    expect(operatorBg).toBeTruthy();
    expect(assistantBg).not.toBe(operatorBg);
  });

  it('adopting the hull did not disturb the quotation', () => {
    // The one assertion here that overlaps commsQuotation, kept because it is
    // THIS lane's regression risk: the hull and the voice live one element
    // apart, and a hull change that swallowed the quoted class would repaint
    // the assistant's words back into the proportional face.
    const hull = bubble('assistant');
    expect(hull.classList.contains(COMMS_TURN_ASSISTANT_CLASS)).toBe(true);
    expect(hull.querySelector(`.${COMMS_QUOTED_CLASS}`)).not.toBeNull();
  });
});
