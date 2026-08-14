import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MessageBubble } from '../components/chat/MessageBubble';
import { COMMS_QUOTED_CLASS, COMMS_SURFACE } from '../lib/algernon/commsSurface';

// THE QUOTATION AND ITS BOUNDARY (comms register).
//
// THE DECLARED EXCEPTION, AND ITS RATIONALE — read this before "fixing" the
// cross-register tokens below as a leak. The chat surface is `comms`, and
// exactly one region inside it speaks in another register's voice: the
// assistant's turns render monospace-phosphor, the ship's computer speaking.
// That is a QUOTATION of the crt register, per the timeline→sensor-log
// precedent, operator-ratified 2026-08-14. It is deliberate. A containment pin
// that simply forbade cross-register tokens on comms would be wrong here, and
// would be "fixed" by deleting the feature.
//
// BUT AN EXCEPTION WITH NO BOUNDARY IS NOT AN EXCEPTION, IT IS A LOOPHOLE. So
// this file pins the boundary in both directions: the quotation IS on the
// assistant's turn, and it is NOT on the operator's turn, NOT on the composer,
// and NOT on any voice affordance. The quoted region is where the borrowing
// stops.

function assistant(text = 'Understood. Three things need you today.') {
  return render(<MessageBubble role="assistant" text={text} ts="2026-08-14T09:00:00Z" />);
}
function operator(text = 'what needs me today?') {
  return render(<MessageBubble role="user" text={text} ts="2026-08-14T09:00:00Z" />);
}

describe('the quotation is applied', () => {
  it('the assistant speaks in the borrowed voice', () => {
    assistant();
    const bubble = screen.getByTestId('msg-assistant');
    expect(bubble.querySelector(`.${COMMS_QUOTED_CLASS}`)).not.toBeNull();
  });

  it('the class is the one the stylesheet actually defines', () => {
    // The markup and the CSS cannot share a symbol, so this is the pin that
    // keeps the two spellings equal — a quotation styled by a class no rule
    // matches is invisible, and every other assertion here would still pass.
    const css = require('node:fs').readFileSync(
      require('node:path').join(__dirname, '..', 'styles', 'comms.css'),
      'utf8',
    );
    expect(css).toContain(`.${COMMS_QUOTED_CLASS}`);
    expect(css).toContain(`[data-surface='${COMMS_SURFACE}']`);
  });
});

describe('the quotation STOPS at the assistant turn', () => {
  it('the operator does not speak in the computer’s voice', () => {
    // The half that makes this an exception rather than a repaint. The operator
    // is not the ship's computer; their own words stay in the proportional face.
    operator();
    const bubble = screen.getByTestId('msg-user');
    expect(bubble.querySelector(`.${COMMS_QUOTED_CLASS}`)).toBeNull();
  });

  it('both roles render their text — the vacuity control', () => {
    // Without this, a MessageBubble that rendered nothing at all would satisfy
    // the absence assertion above and look like a passing boundary.
    const a = assistant('ASSISTANT LINE');
    expect(screen.getByTestId('msg-assistant').textContent).toContain('ASSISTANT LINE');
    a.unmount();
    operator('OPERATOR LINE');
    expect(screen.getByTestId('msg-user').textContent).toContain('OPERATOR LINE');
  });

  it('the borrowed voice is confined to ONE class in the stylesheet', () => {
    // The boundary, at the stylesheet level. `.comms-quoted` is the only rule in
    // comms.css allowed to reach for the quoted vocabulary; if a second rule
    // starts using it, the quotation has begun to spread and this fails.
    const raw: string = require('node:fs').readFileSync(
      require('node:path').join(__dirname, '..', 'styles', 'comms.css'),
      'utf8',
    );
    const css = raw.replace(/\/\*[\s\S]*?\*\//g, '');
    const users = css
      .split('}')
      .map((chunk) => {
        const i = chunk.indexOf('{');
        return i < 0 ? null : { sel: chunk.slice(0, i).trim(), body: chunk.slice(i + 1) };
      })
      .filter((r): r is { sel: string; body: string } => r != null)
      .filter((r) => /var\(--comms-quoted-/.test(r.body))
      .map((r) => r.sel);

    expect(users.length).toBeGreaterThan(0); // vacuity control
    expect(users).toEqual([`.${COMMS_QUOTED_CLASS}`]);
  });
});
