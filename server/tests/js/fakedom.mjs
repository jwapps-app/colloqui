// A just-big-enough DOM for testing the client's pure rendering modules under
// Node (no browser, no dependencies). Import this BEFORE any client module.

class FakeText {
  constructor(text) { this.nodeType = 3; this.data = String(text); }
  get textContent() { return this.data; }
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.className = '';
    this.dataset = {};
    this.style = {};
    this.listeners = {};
  }
  appendChild(node) { this.childNodes.push(node); return node; }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  set textContent(text) { this.childNodes = text === '' ? [] : [new FakeText(text)]; }
  set innerHTML(html) { if (html !== '') throw new Error('fake DOM only supports clearing'); this.childNodes = []; }
  get children() { return this.childNodes.filter(c => c.nodeType === 1); }
}

globalThis.document = {
  createElement: tag => new FakeElement(tag),
  createTextNode: text => new FakeText(text),
};
globalThis.window = { matchMedia: () => ({ matches: false }) };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

// Compact, stable rendering of a fake node for assertions:
//   <strong>bold</strong> and text
export function html(node) {
  if (node.nodeType === 3) return node.data;
  const tag = node.tagName.toLowerCase();
  const cls = node.className ? ` class="${node.className}"` : '';
  const href = node.href ? ` href="${node.href}"` : '';
  const checked = tag === 'input' ? (node.checked ? ' checked' : '') : '';
  if (tag === 'input') return `<input${cls}${checked}>`;
  return `<${tag}${cls}${href}>${node.childNodes.map(html).join('')}</${tag}>`;
}

export function find(node, tag) {
  const out = [];
  (function walk(n) {
    if (n.nodeType !== 1) return;
    if (n.tagName === tag.toUpperCase()) out.push(n);
    n.childNodes.forEach(walk);
  })(node);
  return out;
}
