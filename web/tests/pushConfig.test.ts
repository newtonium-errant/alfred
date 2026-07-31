import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { isPushConfigured, isPushEnabled, readVapidConfig } from '../lib/algernon/pushConfig';

// Pins the two inert gates: VAPID-present (subscribe/public-key) and
// PUSH_ENABLED (the poller). Any VAPID key blank/absent → inert; PUSH_ENABLED
// needs BOTH the flag AND vapid.

const KEYS = ['ALGERNON_VAPID_PUBLIC', 'ALGERNON_VAPID_PRIVATE', 'ALGERNON_VAPID_SUBJECT', 'PUSH_ENABLED'];
function clearEnv() {
  for (const k of KEYS) delete process.env[k];
}
function setVapid() {
  process.env.ALGERNON_VAPID_PUBLIC = 'BPub_key';
  process.env.ALGERNON_VAPID_PRIVATE = 'priv_key';
  process.env.ALGERNON_VAPID_SUBJECT = 'mailto:ops@example.com';
}

beforeEach(clearEnv);
afterEach(clearEnv);

describe('readVapidConfig', () => {
  it('is null until ALL three keys are set', () => {
    expect(readVapidConfig()).toBeNull();
    process.env.ALGERNON_VAPID_PUBLIC = 'BPub_key';
    expect(readVapidConfig()).toBeNull();
    process.env.ALGERNON_VAPID_PRIVATE = 'priv_key';
    expect(readVapidConfig()).toBeNull();
    process.env.ALGERNON_VAPID_SUBJECT = 'mailto:ops@example.com';
    expect(readVapidConfig()).toEqual({
      publicKey: 'BPub_key',
      privateKey: 'priv_key',
      subject: 'mailto:ops@example.com',
    });
  });

  it('treats a blank key as absent (inert)', () => {
    setVapid();
    process.env.ALGERNON_VAPID_PRIVATE = '   ';
    expect(readVapidConfig()).toBeNull();
  });
});

describe('isPushConfigured / isPushEnabled', () => {
  it('isPushConfigured tracks the VAPID triple', () => {
    expect(isPushConfigured()).toBe(false);
    setVapid();
    expect(isPushConfigured()).toBe(true);
  });

  it('isPushEnabled needs BOTH PUSH_ENABLED=true AND vapid', () => {
    expect(isPushEnabled()).toBe(false);
    process.env.PUSH_ENABLED = 'true';
    expect(isPushEnabled()).toBe(false); // vapid still missing
    setVapid();
    expect(isPushEnabled()).toBe(true);
  });

  it('PUSH_ENABLED is case-insensitive and false-by-default', () => {
    setVapid();
    expect(isPushEnabled()).toBe(false); // flag absent
    process.env.PUSH_ENABLED = 'TRUE';
    expect(isPushEnabled()).toBe(true);
    process.env.PUSH_ENABLED = 'false';
    expect(isPushEnabled()).toBe(false);
    process.env.PUSH_ENABLED = '1';
    expect(isPushEnabled()).toBe(false); // only "true" counts
  });
});
