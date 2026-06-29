import { describe, expect, it } from 'vitest';
import { createSseParser } from '../lib/algernon/sse';

// Locks the SSE frame parser: status/done/error events, keep-alive comment frames
// skipped, and frames that arrive split across read() chunks reassembled.

describe('createSseParser', () => {
  it('parses a status frame', () => {
    const p = createSseParser();
    const evs = p.push('event: status\ndata: {"phase":"tool","tool":"vault_search"}\n\n');
    expect(evs).toHaveLength(1);
    expect(evs[0].event).toBe('status');
    expect(JSON.parse(evs[0].data)).toEqual({ phase: 'tool', tool: 'vault_search' });
  });

  it('parses a terminal done frame', () => {
    const p = createSseParser();
    const evs = p.push(
      'event: done\ndata: {"reply":"hi","session_key":"k","ts":"t","user_ts":"u"}\n\n',
    );
    expect(evs).toHaveLength(1);
    expect(evs[0].event).toBe('done');
    expect(JSON.parse(evs[0].data).reply).toBe('hi');
  });

  it('parses an error frame', () => {
    const p = createSseParser();
    const evs = p.push('event: error\ndata: {"error":"engine_error"}\n\n');
    expect(evs[0].event).toBe('error');
    expect(JSON.parse(evs[0].data).error).toBe('engine_error');
  });

  it('skips keep-alive comment frames (no event)', () => {
    const p = createSseParser();
    expect(p.push(': keepalive\n\n')).toEqual([]);
    // a comment then a real event in one chunk → only the real event surfaces
    const evs = p.push(': keepalive\n\nevent: done\ndata: {"reply":"x","session_key":"k","ts":"","user_ts":""}\n\n');
    expect(evs).toHaveLength(1);
    expect(evs[0].event).toBe('done');
  });

  it('reassembles a frame split across chunks', () => {
    const p = createSseParser();
    expect(p.push('event: sta')).toEqual([]);
    expect(p.push('tus\ndata: {"phase":"tool"')).toEqual([]);
    const evs = p.push(',"tool":"vault_create"}\n\n');
    expect(evs).toHaveLength(1);
    expect(evs[0].event).toBe('status');
    expect(JSON.parse(evs[0].data).tool).toBe('vault_create');
  });

  it('returns multiple frames delivered in one chunk', () => {
    const p = createSseParser();
    const evs = p.push(
      'event: status\ndata: {"phase":"tool","tool":"vault_search"}\n\n' +
        'event: done\ndata: {"reply":"ok","session_key":"k","ts":"","user_ts":""}\n\n',
    );
    expect(evs.map((e) => e.event)).toEqual(['status', 'done']);
  });

  it('defaults the event name to "message" when only data is present', () => {
    const p = createSseParser();
    const evs = p.push('data: {"hi":1}\n\n');
    expect(evs[0].event).toBe('message');
  });

  it('parses CRLF-delimited frames (\\r\\n\\r\\n boundary + \\r\\n lines)', () => {
    const p = createSseParser();
    const evs = p.push(
      'event: done\r\ndata: {"reply":"hi","session_key":"k","ts":"","user_ts":""}\r\n\r\n',
    );
    expect(evs).toHaveLength(1);
    expect(evs[0].event).toBe('done');
    // No trailing \r leaks into the data payload.
    expect(JSON.parse(evs[0].data).reply).toBe('hi');
  });

  it('skips a CRLF keep-alive comment frame', () => {
    const p = createSseParser();
    expect(p.push(': keepalive\r\n\r\n')).toEqual([]);
  });
});
