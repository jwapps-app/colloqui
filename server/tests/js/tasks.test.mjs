import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { hasTaskLine, isTaskLine, OPEN_TASK_PREFIX, parseTaskLine } from '../../app/static/js/tasks.js';

const { cases } = JSON.parse(fs.readFileSync(new URL('../task_grammar_cases.json', import.meta.url), 'utf8'));

test('the client reads task lines exactly as the shared cases say (same file the server test reads)', () => {
  for (const c of cases) {
    assert.equal(isTaskLine(c.line), c.task, `isTaskLine(${JSON.stringify(c.line)})`);
    const parsed = parseTaskLine(c.line);
    if (!c.task) { assert.equal(parsed, null, c.line); continue; }
    assert.deepEqual(parsed, { done: c.done, text: c.text }, c.line);
    // "Open task" is what the server counts: unchecked, with something after the box.
    assert.equal(!parsed.done && parsed.text.length > 0, c.open, `open: ${JSON.stringify(c.line)}`);
  }
});

test('hasTaskLine looks at every line of a body', () => {
  assert.equal(hasTaskLine('hello\n[ ] one'), true);
  assert.equal(hasTaskLine('hello\n [ ] not at line start'), false);
  assert.equal(hasTaskLine(''), false);
  assert.equal(hasTaskLine(undefined), false);
});

test('a new task line starts with the open prefix', () => {
  assert.equal(isTaskLine(OPEN_TASK_PREFIX + 'x'), true);
});
