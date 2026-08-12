import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EvidenceBody } from '../components/feed/EvidenceBody';

// Pins the EvidenceBody render for the email-tier card (#26): the XSS-pinned prose
// (email bodies are attacker-controlled text), the scheme-gated "Open in Gmail" anchor
// (href VERBATIM from the server, never client-built; hostile → no anchor), and the
// adaptive truncation copy (email → Gmail, digest → Brief).

const GMAIL = 'https://mail.google.com/mail/u/0/#search/rfc822msgid:a%40b.com';

describe('EvidenceBody — body + external link render', () => {
  it('renders the body as ESCAPED text, never markup (XSS pin, hostile body)', () => {
    const evil = '<img src=x onerror=alert(1)>';
    render(<EvidenceBody evidence={{ body: evil }} />);
    expect(screen.getByText(evil)).toBeTruthy(); // present as literal text
    expect(document.querySelector('img')).toBeNull(); // never injected as markup
  });

  it('renders a server Gmail URL as an anchor with the href VERBATIM + safe rel/target', () => {
    render(<EvidenceBody evidence={{ body: 'preview', gmail_url: GMAIL }} />);
    const a = screen.getByTestId('evidence-external-link');
    expect(a.getAttribute('href')).toBe(GMAIL); // verbatim — no client URL-building
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer');
    expect(a.textContent).toContain('Open in Gmail');
  });

  it('renders NO anchor for a hostile / non-Gmail URL (body still shows)', () => {
    render(<EvidenceBody evidence={{ body: 'preview', gmail_url: 'javascript:alert(1)' }} />);
    expect(screen.queryByTestId('evidence-external-link')).toBeNull();
    expect(screen.getByText('preview')).toBeTruthy();
    // The hostile string never becomes an href anywhere.
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it('truncation copy points to GMAIL when there is a link (#26)', () => {
    render(<EvidenceBody evidence={{ body: 'clipped', body_truncated: true, gmail_url: GMAIL }} />);
    expect(screen.getByTestId('evidence-truncated').textContent).toContain('open in Gmail');
  });

  it('truncation copy points to the BRIEF for a link-less digest (generic path unchanged)', () => {
    render(<EvidenceBody evidence={{ body: 'clipped', truncated: true }} />);
    expect(screen.getByTestId('evidence-truncated').textContent).toContain('the Brief');
  });

  it('a truncated email with NO gmail_url says "Preview only" — NOT the false Brief promise (#26)', () => {
    // Long body + missing message_id ⟹ blank gmail_url ⟹ no link. The Brief never renders
    // email bodies, so "full text in the Brief" would be a meaning-drift lie.
    render(
      <EvidenceBody
        evidence={{ body: 'clipped email', truncated: true, sender: 'a@b.com', subject: 'Re: hi', classifier_priority: 'high', gmail_url: '' }}
      />,
    );
    const copy = screen.getByTestId('evidence-truncated').textContent ?? '';
    expect(copy).toContain('Preview only');
    expect(copy).not.toContain('Brief'); // never the false promise
    expect(screen.queryByTestId('evidence-external-link')).toBeNull(); // no link, as set up
  });

  it('renders nothing when there is neither a body nor a valid link', () => {
    const { container } = render(<EvidenceBody evidence={{ sender: 'a@b.com' }} />);
    expect(container.firstChild).toBeNull();
  });

  describe('surface skins — a shared component must not crash its host', () => {
    // WARN-2. This component takes the palette as a prop because it renders
    // inside BOTH the console deck card and the not-yet-adopted feed row, and
    // `surface` accepts any name — a surface owns its skin in its own
    // stylesheet and adopts the prop without waiting on a change here.
    //
    // The regression this pins was introduced, not inherited: while the prop's
    // type was a closed two-value union, every legal value had a skin entry and
    // the bare `SKIN[surface]` lookup could not miss. OPENING that union (so
    // the feed could adopt the seam) widened this component's input domain
    // without widening its lookup — and did it invisibly, because
    // `Record<open-union, T>` degenerates to a string index signature, so the
    // compiler stopped reporting the gap at the same moment the gap appeared.
    it('falls back to the warm skin for a surface it has never heard of', () => {
      // The exact call that threw: a real name from a real sibling surface.
      const { container } = render(<EvidenceBody evidence={{ body: 'text' }} surface="sensor-log" />);
      expect(container.querySelector('[data-testid="evidence-body"]')).not.toBeNull();
    });

    it('gives that unknown surface the WARM skin, not the console one', () => {
      // Rendering without throwing is necessary but not sufficient: falling
      // back to the wrong skin would paint console colours onto a light
      // surface. Compared against a warm render rather than a hardcoded class
      // list, so this cannot drift from whatever warm actually is.
      const unknown = render(<EvidenceBody evidence={{ body: 'text' }} surface="a-surface-nobody-registered" />);
      const unknownClass = unknown.container.querySelector('[data-testid="evidence-body"] div')?.className;
      unknown.unmount();

      const warm = render(<EvidenceBody evidence={{ body: 'text' }} />);
      const warmClass = warm.container.querySelector('[data-testid="evidence-body"] div')?.className;
      warm.unmount();

      expect(unknownClass).toBe(warmClass);

      // Positive control: console really is a DIFFERENT skin, so "unknown ===
      // warm" is a fact about the fallback and not about every skin being
      // identical.
      const consoleR = render(<EvidenceBody evidence={{ body: 'text' }} surface="console" />);
      const consoleClass = consoleR.container.querySelector('[data-testid="evidence-body"] div')?.className;
      consoleR.unmount();
      expect(consoleClass).not.toBe(warmClass);
    });
  });
});
