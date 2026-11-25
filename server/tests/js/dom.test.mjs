import assert from 'node:assert/strict';
import test from 'node:test';

import { html } from './fakedom.mjs';

const { el, emptyRow, fillList, titledRow } = await import('../../app/static/js/dom.js');

test('el builds an element from props and children', () => {
  let clicked = 0;
  const node = el('li', { className: 'row', onclick: () => { clicked++; }, dataset: { id: '7' } },
    'text ', el('b', null, 'bold'), null, false, [el('i', null, 'a'), 'b']);
  assert.equal(html(node), '<li class="row">text <b>bold</b><i>a</i>b</li>');
  assert.equal(node.dataset.id, '7');
  node.onclick();
  assert.equal(clicked, 1);
});

test('el skips null and undefined props', () => {
  const node = el('div', { onclick: null, title: undefined });
  assert.equal('onclick' in node, false);
  assert.equal('title' in node, false);
});

test('the shared rows', () => {
  assert.equal(html(emptyRow('Nothing yet.')), '<li class="sub">Nothing yet.</li>');
  assert.equal(html(emptyRow('None.', 'empty')), '<li class="empty">None.</li>');
  assert.equal(html(titledRow('Ada', '@ada · admin')),
    '<li><span class="grow">Ada<div class="sub">@ada · admin</div></span></li>');
});

test('fillList replaces rows and shows the empty state', () => {
  const list = el('ul', null, el('li', null, 'stale'));
  fillList(list, ['a', 'b'], { empty: 'none', row: x => el('li', null, x) });
  assert.equal(html(list), '<ul><li>a</li><li>b</li></ul>');
  fillList(list, [], { empty: 'none', emptyClass: 'empty', row: x => el('li', null, x) });
  assert.equal(html(list), '<ul><li class="empty">none</li></ul>');
});
