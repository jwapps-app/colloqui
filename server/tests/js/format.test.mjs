import assert from 'node:assert/strict';
import test from 'node:test';

import { fmtSize, plural, relTime } from '../../app/static/js/format.js';

test('plural', () => {
  assert.equal(plural(0, 'reply', 'replies'), '0 replies');
  assert.equal(plural(1, 'reply', 'replies'), '1 reply');
  assert.equal(plural(2, 'reply', 'replies'), '2 replies');
});

test('fmtSize', () => {
  assert.equal(fmtSize(512), '512 B');
  assert.equal(fmtSize(2048), '2 KB');
  assert.equal(fmtSize(5 * 1024 * 1024), '5.0 MB');
});

test('relTime for the recent past', () => {
  assert.equal(relTime(new Date(Date.now() - 5_000).toISOString()), 'now');
  assert.equal(relTime(new Date(Date.now() - 5 * 60_000).toISOString()), '5m ago');
});
