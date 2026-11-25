import { api } from './api.js';
import { $, touchKeyboard } from './dom.js';
import { maybeSendTyping } from './realtime.js';
import { state } from './state.js';
import { isTaskLine, OPEN_TASK_PREFIX } from './tasks.js';

export let editingMessageId = null;

// The contenteditable box shows live bold/italic/code; on send it serializes
// to the same markdown-ish plain text the renderer understands, so storage,
// search, and the future iOS app all keep working on plain text.

export function setComposerEnabled(on) {
  $('send-input').setAttribute('contenteditable', on ? 'true' : 'false');
  $('send-btn').disabled = !on;
  $('attach-btn').disabled = !on;
  document.querySelectorAll('#toolbar button').forEach(b => { b.disabled = !on; });
}

function serializeNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent.replace(/​/g, '');
  if (node.nodeType !== Node.ELEMENT_NODE) return '';
  const inner = [...node.childNodes].map(serializeNode).join('');
  switch (node.tagName) {
    case 'BR': return '\n';
    case 'B': case 'STRONG': return inner.trim() ? `**${inner}**` : inner;
    case 'I': case 'EM': return inner.trim() ? `*${inner}*` : inner;
    case 'S': case 'STRIKE': case 'DEL': return inner.trim() ? `~~${inner}~~` : inner;
    case 'CODE': return inner.trim() ? `\`${inner}\`` : inner;
    case 'DIV': case 'P': return '\n' + inner;
    default: return inner;
  }
}

export function serializeEditor() {
  return [...$('send-input').childNodes].map(serializeNode).join('')
    .replace(/\u00a0/g, ' ')
    .trim();
}

export function clearEditor() { $('send-input').innerHTML = ''; }

// Per-channel unsent drafts, kept in localStorage.
// Drafts are keyed by account AND channel: keyed by channel alone, the next
// person to sign in on a shared device would have the last one's unsent text
// restored into the same channel's composer.
function draftKey(channelId) { return `draft:${state.me ? state.me.id : 'anon'}:${channelId}`; }

function saveDraft() {
  if (!state.currentChannel || editingMessageId) return;
  const content = serializeEditor();
  if (content) localStorage.setItem(draftKey(state.currentChannel.id), content);
  else localStorage.removeItem(draftKey(state.currentChannel.id));
}
export function restoreDraft(channelId) {
  clearEditor();
  const d = localStorage.getItem(draftKey(channelId));
  if (d) fillEditor(d);
}
export function clearDraft(channelId) { localStorage.removeItem(draftKey(channelId)); }

function updateToolbar() {
  for (const cmd of ['bold', 'italic', 'strikeThrough']) {
    const btn = document.querySelector(`#toolbar button[data-cmd="${cmd}"]`);
    let on = false;
    try { on = document.queryCommandState(cmd); } catch {}
    if (btn) btn.classList.toggle('active', on);
  }
}

function toggleCode() {
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  const box = $('send-input');
  if (!box.contains(range.commonAncestorContainer)) return;
  let n = range.commonAncestorContainer;
  while (n && n !== box) {
    if (n.nodeType === 1 && n.tagName === 'CODE') {
      const parent = n.parentNode;
      while (n.firstChild) parent.insertBefore(n.firstChild, n);
      parent.removeChild(n);
      return;
    }
    n = n.parentNode;
  }
  if (range.collapsed) {
    // No selection: drop in an empty code span and put the caret inside it
    // (zero-width char gives the inline element something to hold the caret).
    const code = document.createElement('code');
    code.appendChild(document.createTextNode('​'));
    range.insertNode(code);
    const r = document.createRange();
    r.setStart(code.firstChild, 1);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
    return;
  }
  const code = document.createElement('code');
  try {
    range.surroundContents(code);
  } catch {
    code.textContent = range.toString();
    range.deleteContents();
    range.insertNode(code);
  }
}

function execFormat(cmd) {
  $('send-input').focus();
  if (cmd === 'code') toggleCode();
  else document.execCommand(cmd);
  updateToolbar();
}

export const memberCache = new Map();
let mentionState = null;   // { node, atOffset, endOffset }
let mentionItems = [];
let mentionIndex = 0;

async function membersFor(channelId) {
  if (memberCache.has(channelId)) return memberCache.get(channelId);
  let members = [];
  try { members = await api(`/channels/${channelId}/members`); } catch {}
  memberCache.set(channelId, members);
  return members;
}

export function closeMention() {
  mentionState = null;
  $('mention-box').classList.add('hidden');
}

async function updateMention() {
  const sel = window.getSelection();
  if (!state.currentChannel || !sel.rangeCount || !sel.isCollapsed) return closeMention();
  const node = sel.anchorNode;
  const offset = sel.anchorOffset;
  if (!node || node.nodeType !== Node.TEXT_NODE) return closeMention();
  const before = node.textContent.slice(0, offset);
  const m = before.match(/(^|\s)@([a-z0-9_]*)$/i);
  if (!m) return closeMention();
  const query = m[2].toLowerCase();
  const atOffset = offset - m[2].length - 1;  // position of '@'
  const members = await membersFor(state.currentChannel.id);
  mentionItems = members.filter(u =>
    u.username.toLowerCase().startsWith(query)
    || u.display_name.toLowerCase().includes(query)).slice(0, 6);
  if (!mentionItems.length) return closeMention();
  mentionIndex = 0;
  mentionState = { node, atOffset, endOffset: offset };
  renderMentionBox();
}

function renderMentionBox() {
  const box = $('mention-box');
  box.innerHTML = '';
  mentionItems.forEach((u, i) => {
    const item = document.createElement('div');
    item.className = 'mention-item' + (i === mentionIndex ? ' active' : '');
    item.appendChild(document.createTextNode(u.display_name));
    const uname = document.createElement('span');
    uname.className = 'uname';
    uname.textContent = '@' + u.username;
    item.appendChild(uname);
    item.onmousedown = e => { e.preventDefault(); chooseMention(i); };
    box.appendChild(item);
  });
  const sel = window.getSelection();
  if (sel.rangeCount) {
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    box.style.left = Math.min(rect.left, window.innerWidth - 290) + 'px';
    box.style.bottom = (window.innerHeight - rect.top + 6) + 'px';  // above the caret
  }
  box.classList.remove('hidden');
}

function chooseMention(i) {
  if (!mentionState) return;
  const u = mentionItems[i];
  const { node, atOffset, endOffset } = mentionState;
  const range = document.createRange();
  range.setStart(node, atOffset);
  range.setEnd(node, endOffset);
  range.deleteContents();
  const text = document.createTextNode('@' + u.username + ' ');
  range.insertNode(text);
  const sel = window.getSelection();
  sel.removeAllRanges();
  const after = document.createRange();
  after.setStartAfter(text);
  after.collapse(true);
  sel.addRange(after);
  closeMention();
  $('send-input').focus();
}

function currentLineText() {
  const s = window.getSelection();
  if (!s.rangeCount || !s.anchorNode || s.anchorNode.nodeType !== Node.TEXT_NODE) return '';
  const upto = s.anchorNode.textContent.slice(0, s.anchorOffset);
  return upto.slice(upto.lastIndexOf('\n') + 1);
}

function insertPrefix(prefix) {
  const box = $('send-input');
  box.focus();
  const s = window.getSelection();
  let before = '';
  if (s.rangeCount && s.anchorNode && s.anchorNode.nodeType === Node.TEXT_NODE) {
    before = s.anchorNode.textContent.slice(0, s.anchorOffset);
  }
  const atLineStart = before === '' || before.endsWith('\n');
  document.execCommand('insertText', false, (atLineStart ? '' : '\n') + prefix);
}

// Edit reuses the main composer (full toolbar) in an "edit mode".
function fillInlineEditor(text, container) {
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(~~[^~\n]+~~)|(\*[^*\n]+\*)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    container.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    let el, inner;
    if (m[1]) { el = document.createElement('code'); inner = tok.slice(1, -1); }
    else if (m[2]) { el = document.createElement('strong'); inner = tok.slice(2, -2); }
    else if (m[3]) { el = document.createElement('del'); inner = tok.slice(2, -2); }
    else { el = document.createElement('em'); inner = tok.slice(1, -1); }
    el.textContent = inner;
    container.appendChild(el);
    last = m.index + tok.length;
  }
  container.appendChild(document.createTextNode(text.slice(last)));
}

function fillEditor(text) {
  const el = $('send-input');
  el.innerHTML = '';
  text.split('\n').forEach((line, i) => {
    if (i) el.appendChild(document.createElement('br'));
    fillInlineEditor(line, el);
  });
}

export function editMessage(m) {
  if (!state.currentChannel) return;
  editingMessageId = m.id;
  setComposerEnabled(true);
  fillEditor(m.content);
  $('send-input').focus();
  $('edit-banner').classList.remove('hidden');
  $('send-btn').textContent = 'Save';
  $('attach-btn').disabled = true;
}

export function exitEditMode() {
  if (!editingMessageId) return;
  editingMessageId = null;
  clearEditor();
  $('edit-banner').classList.add('hidden');
  $('send-btn').textContent = 'Send';
  $('attach-btn').disabled = !state.currentChannel;
}

// Connect the composer's own controls: typing, mentions, list continuation,
// formatting shortcuts, plain-text paste and the toolbar.
export function wireComposer() {
  $('send-input').oninput = () => {
    maybeSendTyping();
    updateMention();
    saveDraft();
    // Restore the placeholder when the box is visually empty (browsers can
    // leave a stray <br> behind that defeats the :empty CSS selector).
    const box = $('send-input');
    if (box.innerText.trim() === '' && !box.querySelector('code,b,strong,i,em')) {
      box.innerHTML = '';
    }
  };
  $('send-input').addEventListener('blur', () => setTimeout(closeMention, 150));
  // Desktop: Enter sends, Shift+Enter inserts a newline.
  // Touch keyboards: Enter inserts a newline; the Send button submits.
  $('send-input').addEventListener('keydown', e => {
    // Mention dropdown captures navigation keys first.
    if (mentionState) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        mentionIndex = (mentionIndex + 1) % mentionItems.length;
        renderMentionBox(); return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        mentionIndex = (mentionIndex - 1 + mentionItems.length) % mentionItems.length;
        renderMentionBox(); return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault(); chooseMention(mentionIndex); return;
      }
      if (e.key === 'Escape') { e.preventDefault(); closeMention(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey && !touchKeyboard) {
      e.preventDefault();
      $('send-form').requestSubmit();
      return;
    }
    if (e.key === 'Enter' && (e.shiftKey || touchKeyboard)) {
      // Continue lists: a new line after "- item" or "[ ] task" starts
      // pre-prefixed; an empty list line ends the list instead.
      const line = currentLineText();
      let prefix = null;
      let num;
      if (isTaskLine(line)) prefix = OPEN_TASK_PREFIX;
      else if (/^- /.test(line)) prefix = '- ';
      else if ((num = line.match(/^(\d+)\. /))) prefix = (parseInt(num[1], 10) + 1) + '. ';
      if (prefix) {
        e.preventDefault();
        // An empty list item (just the marker) ends the list instead of continuing.
        const empty = /^(\[( |x)\]|-|\d+\.)\s*$/i.test(line.trim());
        if (empty) {
          document.execCommand('insertText', false, '\n');
        } else {
          document.execCommand('insertText', false, '\n' + prefix);
        }
        return;
      }
    }
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey) {
      const key = e.key.toLowerCase();
      if (key === 'b') { e.preventDefault(); execFormat('bold'); }
      else if (key === 'i') { e.preventDefault(); execFormat('italic'); }
    }
  });
  // Paste as plain text — formatting comes only from our own toolbar.
  $('send-input').addEventListener('paste', e => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text/plain');
    document.execCommand('insertText', false, text);
  });
  document.querySelectorAll('#toolbar button').forEach(b => {
    // mousedown would steal focus and collapse the selection before the
    // command runs — suppress it so formatting applies to the selected text.
    b.onmousedown = e => e.preventDefault();
    b.onclick = () => {
      if (b.dataset.insert) insertPrefix(b.dataset.insert);
      else execFormat(b.dataset.cmd || 'code');
    };
  });
  document.addEventListener('selectionchange', updateToolbar);
}
