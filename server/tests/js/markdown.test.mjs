import assert from 'node:assert/strict';
import test from 'node:test';

import { find, html } from './fakedom.mjs';

const { renderBody, renderInline } = await import('../../app/static/js/markdown.js');
const { state } = await import('../../app/static/js/state.js');

function inline(text) {
  const box = document.createElement('p');
  renderInline(text, box);
  return box.childNodes.map(html).join('');
}

test('inline formatting', () => {
  assert.equal(inline('plain'), 'plain');
  assert.equal(inline('a **bold** b'), 'a <strong>bold</strong> b');
  assert.equal(inline('an *italic* word'), 'an <em>italic</em> word');
  assert.equal(inline('~~gone~~'), '<del>gone</del>');
  assert.equal(inline('run `ls -la` now'), 'run <code>ls -la</code> now');
  // Code wins over the markers inside it.
  assert.equal(inline('`**not bold**`'), '<code>**not bold**</code>');
});

test('links: markdown links, bare URLs, and trailing punctuation stays outside', () => {
  assert.equal(inline('[docs](https://example.com/a)'), '<a href="https://example.com/a">docs</a>');
  assert.equal(inline('see https://example.com/x.'), 'see <a href="https://example.com/x">https://example.com/x</a>.');
  assert.equal(inline('(https://example.com)'), '(<a href="https://example.com">https://example.com</a>)');
  // Nothing but http(s) ever becomes a link.
  assert.equal(inline('[x](javascript:alert(1))'), '[x](javascript:alert(1))');
});

test('text is never interpreted as HTML', () => {
  const box = document.createElement('p');
  renderInline('<img src=x onerror=alert(1)>', box);
  assert.equal(box.children.length, 0);                 // no element was created
  assert.equal(box.textContent, '<img src=x onerror=alert(1)>');
});

test('my own @mention is highlighted, other names are not', () => {
  state.me = { username: 'ada' };
  try {
    assert.equal(inline('hi @ada and @ben'), 'hi <span class="mention">@ada</span> and @ben');
  } finally { state.me = null; }
  assert.equal(inline('hi @ada'), 'hi @ada');
});

test('blocks: bullets, numbers, quotes, code fences', () => {
  const body = renderBody('- one\n2. two\n> quoted\n```\nlet x = **1**;\n```\ntail');
  assert.equal(html(body),
    '<div class="body">'
    + '<div class="bullet-line"><span class="bullet">•</span><span>one</span></div>'
    + '<div class="bullet-line"><span class="bullet">2.</span><span>two</span></div>'
    + '<div class="blockquote-line">quoted</div>'
    + '<pre class="code-block">let x = **1**;</pre>'
    + 'tail</div>');
});

test('an unclosed code fence is shown as text, not swallowed', () => {
  assert.equal(renderBody('```\nstill here').textContent, '```\nstill here');
});

test('task lines render as checkboxes that report their line', async () => {
  const calls = [];
  const body = renderBody('intro\n[ ] open one\n[x] done one', { 2: '2026-01-02T00:00:00Z' },
    async (line, checked) => { calls.push([line, checked]); });
  const boxes = find(body, 'input');
  assert.deepEqual(boxes.map(b => b.checked), [false, true]);
  assert.equal(find(body, 'div').filter(d => d.className === 'task-line done').length, 1);
  assert.match(body.textContent, /cleared /);            // the "cleared <date>" stamp on the done line

  boxes[0].checked = true;                               // the user ticks it
  boxes[0].onchange();
  assert.deepEqual(calls, [[1, true]]);                  // line index within the message
});

test('a failed toggle flips the box back', async () => {
  const body = renderBody('[ ] flaky', {}, async () => { throw new Error('offline'); });
  const [box] = find(body, 'input');
  box.checked = true;
  box.onchange();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(box.checked, false);
});

test('without a toggle handler the boxes are read-only', () => {
  const [box] = find(renderBody('[ ] look only'), 'input');
  assert.equal(box.disabled, true);
});
