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
});
