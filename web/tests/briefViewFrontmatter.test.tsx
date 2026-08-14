import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BriefView, stripFrontmatter } from '../components/brief/BriefView';

// THE OPERATOR READ HIS BRIEF'S YAML.
//
// The daemons spool their markdown with the record's frontmatter attached, and
// this view renders markdown as ESCAPED TEXT — so `type: run`, `status:
// completed` and the rest arrived as the opening lines of the morning brief.
// Machine metadata rendered as prose, on the surface that replaced the brief
// page, in front of the person it is written for.
//
// The stripper is deliberately narrow, and these pins hold it there: it removes
// a block that OPENS ON THE FIRST LINE and nothing else. The failure direction
// is showing too much, never eating the document — a brief with a stray `---`
// must not lose its second half.

const FM = ['---', 'type: run', 'status: completed', 'date: 2026-08-14', '---'].join('\n');

describe('stripFrontmatter', () => {
  it('removes a leading block and keeps the body', () => {
    const out = stripFrontmatter(`${FM}\n# Morning Brief\n\nThree things today.`);
    expect(out).toBe('# Morning Brief\n\nThree things today.');
    expect(out).not.toContain('status: completed');
  });

  it('leaves a document with no frontmatter untouched', () => {
    const doc = '# Morning Brief\n\nNothing above me.';
    expect(stripFrontmatter(doc)).toBe(doc);
  });

  it('does NOT touch a mid-document rule', () => {
    // `---` as a horizontal rule is ordinary markdown. Only a block opening on
    // line 1 is frontmatter.
    const doc = '# Brief\n\nAbove.\n\n---\n\nBelow.';
    expect(stripFrontmatter(doc)).toBe(doc);
    expect(stripFrontmatter(doc)).toContain('Below.');
  });

  it('returns an UNTERMINATED opening fence unchanged rather than eating the doc', () => {
    // A truncated artifact, or a document that simply starts with a rule. The
    // safe direction is showing too much; swallowing the brief is the one
    // outcome worse than showing its YAML.
    const doc = '---\n# Brief\n\nEverything after an unclosed fence.';
    expect(stripFrontmatter(doc)).toBe(doc);
    expect(stripFrontmatter(doc)).toContain('Everything after');
  });

  it('handles a frontmatter-only artifact without crashing', () => {
    expect(stripFrontmatter(FM)).toBe('');
  });

  it('is not vacuous — it really does remove something', () => {
    // Positive control: every "unchanged" assertion above would pass against a
    // function that returns its input.
    const before = `${FM}\nbody`;
    expect(stripFrontmatter(before).length).toBeLessThan(before.length);
  });
});

describe('BriefView renders the brief, not its metadata', () => {
  it('shows the body and none of the frontmatter keys', () => {
    render(
      <BriefView
        testId="brief-view"
        title="Morning Brief"
        date="2026-08-14"
        markdown={`${FM}\n# Morning Brief\n\nThree things today.`}
        emptyMessage="nothing yet"
      />,
    );
    const text = screen.getByTestId('brief-view').textContent || '';
    expect(text).toContain('Three things today.');
    for (const leaked of ['type: run', 'status: completed', 'date: 2026-08-14']) {
      // The date DOES render — as the view's own dateline, from the `date`
      // prop. What must not appear is the raw `date:` KEY from the YAML.
      expect(text).not.toContain(leaked);
    }
  });

  it('still renders a plain brief unchanged — the vacuity control', () => {
    render(
      <BriefView
        testId="plain-view"
        title="Daily Sync"
        date="2026-08-14"
        markdown="# Daily Sync\n\nAll quiet."
        emptyMessage="nothing yet"
      />,
    );
    expect(screen.getByTestId('plain-view').textContent).toContain('All quiet.');
  });
});
