import { describe, expect, it } from 'vitest';
import { safeNextPath } from '../lib/algernon/safeNextPath';

// The open-redirect guard — only same-origin relative paths survive; everything
// else falls back to '/'. Mirrors honeydew's signin guard cases.
describe('safeNextPath', () => {
  it('keeps a simple same-origin path', () => {
    expect(safeNextPath('/')).toBe('/');
    expect(safeNextPath('/chat')).toBe('/chat');
    expect(safeNextPath('/a/b?c=d')).toBe('/a/b?c=d');
  });

  it('falls back to / for empty / non-string', () => {
    expect(safeNextPath('')).toBe('/');
    expect(safeNextPath(undefined)).toBe('/');
    expect(safeNextPath(null)).toBe('/');
    expect(safeNextPath(42)).toBe('/');
  });

  it('rejects a path not rooted at a single /', () => {
    expect(safeNextPath('chat')).toBe('/');
    expect(safeNextPath('https://evil.com')).toBe('/');
  });

  it('rejects protocol-relative and backslash tricks', () => {
    expect(safeNextPath('//evil.com')).toBe('/');
    expect(safeNextPath('/\\evil.com')).toBe('/');
    expect(safeNextPath('/foo\\bar')).toBe('/');
  });

  it('rejects control / whitespace characters', () => {
    expect(safeNextPath('/foo\tbar')).toBe('/');
    expect(safeNextPath('/foo\nbar')).toBe('/');
    expect(safeNextPath('/ leading-space')).toBe('/');
  });
});
