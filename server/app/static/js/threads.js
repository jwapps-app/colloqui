import { api } from './api.js';
import { appAlert } from './dialogs.js';
import { $, el, keyboardActivatable } from './dom.js';
import { closePreview } from './files.js';
import { relTime } from './format.js';
import { closeInfoPane } from './infopane.js';
import { buildMessageNode } from './messages.js';
import { facesEl, replyCount } from './rows.js';
import { state } from './state.js';

export let threadRootId = null;

export function threadSummaryEl(m) {
  const node = el('div', { className: 'thread-summary', onclick: () => openThread(m) },
    facesEl(m.thread_repliers),
    el('span', null, replyCount(m.reply_count)),
    m.thread_last_at ? el('span', { className: 'last' }, '· ' + relTime(m.thread_last_at)) : null);
  keyboardActivatable(node, () => openThread(m));
  return node;
}

// Refresh (or remove) a root message's "N replies" affordance in the timeline.
export function updateThreadSummary(rootId, meta) {
  const node = document.querySelector(`#messages .msg[data-id="${rootId}"] .msg-main`);
  if (!node) return;
  const existing = node.querySelector('.thread-summary');
  if (existing) existing.remove();
  if (meta.reply_count > 0) {
    node.appendChild(threadSummaryEl({
      id: rootId,
      channel_id: meta.channel_id,
      reply_count: meta.reply_count,
      thread_last_at: meta.thread_last_at,
      thread_repliers: meta.thread_repliers || [],
    }));
  }
}

export async function openThread(m) {
  const rootId = m.thread_root_id || m.id;
  threadRootId = rootId;
  closePreview();
  closeInfoPane();
  $('thread-pane').classList.remove('hidden');
  // Styled through the DOM, not a style attribute: the CSP forbids inline styles.
  $('thread-content').replaceChildren(
    el('div', { className: 'empty', style: { color: 'var(--muted)', padding: '12px' } }, 'Loading…'));
  try {
    const msgs = await api(`/messages/${rootId}/thread`);
    if (threadRootId !== rootId) return;  // user moved on while loading
    renderThread(msgs);
  } catch (err) {
    $('thread-content').innerHTML = '';
    appAlert(err.message);
  }
}

function renderThread(msgs) {
  const box = $('thread-content');
  box.innerHTML = '';
  if (!msgs.length) { closeThread(); return; }
  const root = buildMessageNode(msgs[0], { inThread: true });
  root.classList.add('thread-root-msg');
  box.appendChild(root);
  const replies = msgs.slice(1);
  const count = document.createElement('div');
  count.id = 'thread-count';
  count.textContent = replies.length
    ? replyCount(replies.length)
    : 'No replies yet — start one below';
  box.appendChild(count);
  replies.forEach(r => box.appendChild(buildMessageNode(r, { inThread: true })));
  box.scrollTop = box.scrollHeight;
  $('thread-input').focus();
}

export function appendThreadReply(m) {
  const box = $('thread-content');
  box.appendChild(buildMessageNode(m, { inThread: true }));
  const c = $('thread-count');
  if (c) {
    const n = box.querySelectorAll('.msg').length - 1;  // exclude the root
    c.textContent = replyCount(n);
  }
  box.scrollTop = box.scrollHeight;
}

export function closeThread() {
  threadRootId = null;
  $('thread-pane').classList.add('hidden');
}

export function autoGrowThread() {
  const t = $('thread-input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 140) + 'px';
}

export async function sendThreadReply(e) {
  e.preventDefault();
  const ta = $('thread-input');
  const content = ta.value.trim();
  if (!content || !threadRootId || !state.currentChannel) return;
  if (content.length > 4000) { appAlert('Message is too long (4000 characters max)'); return; }
  ta.value = '';
  autoGrowThread();
  try {
    await api(`/channels/${state.currentChannel.id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content, thread_root_id: threadRootId }),
    });
  } catch (err) { ta.value = content; autoGrowThread(); appAlert(err.message); }
}
