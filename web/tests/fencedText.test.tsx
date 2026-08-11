/**
 * #85 — FencedText: the shared renderer, and the four surfaces that use it.
 *
 * The wiring block at the bottom is the load-bearing half. `fencedBlocks.test.ts`
 * and the component tests here both stay fully green against a build where no
 * production surface imports FencedText at all — the feature ships, every pin
 * passes, and the operator never sees a download button. So each surface gets
 * its own render-and-click assertion.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { FencedText } from '../components/markdown/FencedText';

const FENCED = 'Here you go:\n```csv\nname,qty\nwidget,3\n```\nAnything else?';

// jsdom implements neither createObjectURL nor a real download; capture the
// anchor the component builds instead, which is what actually carries the
// filename and the blob.
let clicked: HTMLAnchorElement[] = [];
let revoked: string[] = [];
let blobs: Blob[] = [];

beforeEach(() => {
  clicked = [];
  revoked = [];
  blobs = [];
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (b: Blob) => { blobs.push(b); return `blob:mock-${blobs.length}`; },
    revokeObjectURL: (u: string) => { revoked.push(u); },
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('FencedText — rendering', () => {
  it('emits a single plain block when there is no fence', () => {
    // The no-change guarantee for ordinary messages: same markup as before,
    // no panel, no button.
    render(<FencedText text="just a reply" data-testid="body" />);
    expect(screen.getByTestId('body').textContent).toBe('just a reply');
    expect(screen.queryByTestId('fenced-block')).toBeNull();
  });

  it('splits a fence into its own panel with the surrounding prose intact', () => {
    render(<FencedText text={FENCED} data-testid="body" />);
    expect(screen.getByTestId('fenced-block-content').textContent).toBe(
      'name,qty\nwidget,3',
    );
    const whole = screen.getByTestId('body').textContent ?? '';
    expect(whole).toContain('Here you go:');
    expect(whole).toContain('Anything else?');
    // The backticks themselves are gone — they were never meant to be read.
    expect(whole).not.toContain('```');
  });

  it('labels the block and the button by language', () => {
    render(<FencedText text={FENCED} />);
    expect(screen.getByTestId('fenced-block-lang').textContent).toBe('csv');
    expect(screen.getByTestId('fenced-block-download').textContent).toBe(
      'Download as CSV',
    );
  });

  it('is generic across fence languages, not CSV-only', () => {
    render(<FencedText text={'```json\n{"a":1}\n```'} />);
    expect(screen.getByTestId('fenced-block-download').textContent).toBe(
      'Download as JSON',
    );
  });

  it('renders every fence in a multi-fence message', () => {
    render(<FencedText text={'```csv\na\n```\nmid\n```json\n{}\n```'} />);
    expect(screen.getAllByTestId('fenced-block')).toHaveLength(2);
  });
});

describe('FencedText — the #22 stored-XSS pin still holds', () => {
  it('renders hostile markup as visible literal text, inside a fence', () => {
    // These surfaces carry peer-authored and email-authored text. FencedText
    // chooses which ELEMENT text lands in; it must never render markup.
    const hostile = '<img src=x onerror=alert(1)> <script>alert(2)</script>';
    render(<FencedText text={'```csv\n' + hostile + '\n```'} data-testid="body" />);

    const body = screen.getByTestId('body');
    expect(body.textContent).toContain('<script>');
    expect(body.querySelector('script')).toBeNull();
    expect(body.querySelector('img')).toBeNull();
  });

  it('renders hostile markup as literal text outside a fence too', () => {
    const hostile = '<script>alert(1)</script>';
    render(<FencedText text={hostile} data-testid="body" />);
    expect(screen.getByTestId('body').textContent).toBe(hostile);
    expect(screen.getByTestId('body').querySelector('script')).toBeNull();
  });
});

describe('FencedText — the download itself', () => {
  it('downloads the fence contents, client-side, with no upload', () => {
    render(<FencedText text={FENCED} nameHint="Q3 claims" />);
    fireEvent.click(screen.getByTestId('fenced-block-download'));

    expect(clicked).toHaveLength(1);
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toBe('text/csv;charset=utf-8');
  });

  it('names the file from the caller hint plus the language extension', () => {
    render(<FencedText text={FENCED} nameHint="Q3 claims" />);
    fireEvent.click(screen.getByTestId('fenced-block-download'));
    expect(clicked[0].download).toBe('Q3-claims.csv');
  });

  it('sanitises a hostile name hint before it reaches the anchor', () => {
    render(<FencedText text={FENCED} nameHint="../../etc/passwd" />);
    fireEvent.click(screen.getByTestId('fenced-block-download'));
    expect(clicked[0].download).toBe('etc-passwd.csv');
    expect(clicked[0].download).not.toContain('/');
  });

  it('revokes the object URL after the click', async () => {
    // Deferred by a tick on purpose — revoking synchronously cancels the
    // download in Safari. So this asserts it happens, and happens LATE.
    render(<FencedText text={FENCED} />);
    fireEvent.click(screen.getByTestId('fenced-block-download'));
    expect(revoked).toHaveLength(0);
    await new Promise((r) => setTimeout(r, 1));
    // `toContain`, not `toEqual`: deferred revokes from earlier tests in this
    // file land in the array too. What this pin is about is that the revoke
    // happens and happens LATE — the length-0 assertion above is the "late".
    expect(revoked).toContain('blob:mock-1');
  });

  it('removes the temporary anchor from the document', () => {
    render(<FencedText text={FENCED} />);
    fireEvent.click(screen.getByTestId('fenced-block-download'));
    expect(clicked[0].isConnected).toBe(false);
  });
});

// ===========================================================================
// Wiring — every surface that renders daemon-authored text
// ===========================================================================

describe('the four production surfaces route through FencedText', () => {
  it('chat message bubbles', async () => {
    const { MessageBubble } = await import('../components/chat/MessageBubble');
    render(<MessageBubble role="assistant" text={FENCED} ts="2026-08-11T02:00:00Z" />);
    expect(screen.getByTestId('fenced-block-download').textContent).toBe(
      'Download as CSV',
    );
  });

  it('the brief / daily-sync view', async () => {
    const { BriefView } = await import('../components/brief/BriefView');
    render(
      <BriefView
        title="Morning Brief"
        date="2026-08-11"
        markdown={FENCED}
        testId="brief"
        emptyMessage="nothing yet"
      />,
    );
    expect(screen.getByTestId('fenced-block-download')).toBeTruthy();
  });

  it('the notification tray ticket body', async () => {
    const { NotificationList } = await import('../components/NotificationList');
    render(
      <NotificationList
        notifications={[{
          id: 'n1',
          text: 'New ticket — filed as issue #9',
          precedence: 'R',
          source: 'kal-le',
          ticket_uid: 'tkt-1',
          issue_url: 'http://localhost:3001/x/y/issues/9',
          ts: '2026-08-11T02:00:00Z',
          read: true,
          ticket_body: FENCED,
        } as never]}
        onAck={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('notification-expand'));
    expect(screen.getByTestId('fenced-block-download')).toBeTruthy();
  });

  it('feed card evidence', async () => {
    const { EvidenceBody } = await import('../components/feed/EvidenceBody');
    render(<EvidenceBody evidence={{ body: FENCED }} />);
    expect(screen.getByTestId('fenced-block-download')).toBeTruthy();
  });
});
