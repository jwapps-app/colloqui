// Small DOM helpers. Everything here is free of app state, so any module can
// use it.

export const $ = id => document.getElementById(id);

// Build an element: el('li', { className: 'sub', onclick: fn }, 'text', child).
// Props are assigned as properties (`dataset` and `style` are merged); a
// string child becomes a text node; null/false children are skipped.
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key === 'dataset' || key === 'style') Object.assign(node[key], value);
    else node[key] = value;
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

// The "nothing here" row every list shows when it is empty.
export function emptyRow(text, className = 'sub') {
  return el('li', { className }, text);
}

// The standard settings-list row: a title with a quieter line under it. The
// caller appends the row's action buttons.
export function titledRow(title, sub) {
  return el('li', null,
    el('span', { className: 'grow' }, title, el('div', { className: 'sub' }, sub)));
}

// Replace a list's rows: `row(item)` for each item, or one empty-state row.
export function fillList(list, items, { empty, emptyClass, row }) {
  list.innerHTML = '';
  if (!items.length && empty) list.appendChild(emptyRow(empty, emptyClass));
  for (const item of items) {
    const node = row(item);
    if (node) list.appendChild(node);
  }
}

// Touch keyboards have no cheap "send" key: Enter inserts a newline there and
// the Send button submits. On desktop Enter sends, Shift+Enter is a newline.
export const touchKeyboard = window.matchMedia('(pointer: coarse)').matches;

// Cached MediaQueryList objects — constructing matchMedia() per call (once per
// message node, per click) is wasteful; these update themselves live.
const _mqNarrow = window.matchMedia('(max-width: 700px)');
const _mqHover = window.matchMedia('(hover: hover)');
export function isNarrow() { return _mqNarrow.matches; }
export function hasHover() { return _mqHover.matches; }

let _toastTimer = null;
export function toast(msg) {
  let el = document.getElementById('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1600);
}

export async function copyText(text) {
  // Shared clipboard write with a fallback for non-secure contexts (e.g. http
  // LAN testing). Returns whether the copy succeeded.
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    return true;
  } catch { return false; }
}
// Make a clickable non-button element usable from the keyboard: focusable,
// announced as a button, and activated by Enter or Space.
export function keyboardActivatable(el, fn) {
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); }
  });
}
