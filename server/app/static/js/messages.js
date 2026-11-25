import { api } from './api.js';
import { avatarEl } from './avatars.js';
import { canManage, jumpToMessage } from './channels.js';
import {
  clearDraft,
  clearEditor,
  editingMessageId,
  editMessage,
  exitEditMode,
  serializeEditor,
} from './composer.js';
import { appAlert, appConfirm } from './dialogs.js';
import { $, copyText, hasHover, isNarrow, toast } from './dom.js';
import { downloadFile, loadAuthedBlob, openViewer, previewKind } from './files.js';
import { fmtSize, relTime } from './format.js';
import { svgIcon } from './icons.js';
import { renderBody } from './markdown.js';
import { meta } from './meta.js';
import { refreshTaskCount } from './panes.js';
import { remindAboutMessage } from './reminders.js';
import { state } from './state.js';
import { isTaskLine, OPEN_TASK_PREFIX } from './tasks.js';
import { openThread, threadSummaryEl } from './threads.js';
import { keepMessagesPinned } from './viewport.js';

async function copyMessageText(m) {
  if (!m.content) return;
  toast(await copyText(m.content) ? 'Copied' : 'Could not copy');
}

// Mobile message-action popup, opened by long-pressing a message. Built from
// the same action descriptors the desktop inline icons use, topped with a
// scrollable quick-reaction emoji row (iOS style).
function closeMsgMenu() {
  const m = document.getElementById('msg-menu');
  if (m) m.remove();
  document.removeEventListener('pointerdown', onMsgMenuDown, true);
  window.removeEventListener('scroll', closeMsgMenu, true);
  window.removeEventListener('resize', closeMsgMenu, true);
}
function onMsgMenuDown(e) {
  const menu = document.getElementById('msg-menu');
  if (menu && !menu.contains(e.target)) closeMsgMenu();
}
function openMsgMenu(anchor, acts, m) {
  closeMsgMenu();
  const menu = document.createElement('div');
  menu.id = 'msg-menu';
  menu.className = 'msg-menu';
  if (m) {
    const row = document.createElement('div');
    row.className = 'mm-emoji';
    for (const emoji of meta.emoji) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = emoji;
      b.onclick = () => { closeMsgMenu(); toggleReaction(m.id, emoji); };
      row.appendChild(b);
    }
    menu.appendChild(row);
  }
  for (const a of acts) {
    const item = document.createElement('button');
    const ic = document.createElement('span');
    ic.className = 'mm-icon';
    ic.innerHTML = svgIcon(a.icon, 'act-ico');
    item.appendChild(ic);
    item.appendChild(document.createTextNode(a.label));
    item.onclick = () => { closeMsgMenu(); a.fn(); };
    menu.appendChild(item);
  }
  menu.style.visibility = 'hidden';
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  // Always right-aligned to the message (away from the avatar/thumb side).
  let left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8));
  let top = Math.max(8, Math.min(r.top, window.innerHeight - mh - 8));
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  menu.style.visibility = 'visible';
  setTimeout(() => {
    document.addEventListener('pointerdown', onMsgMenuDown, true);
    window.addEventListener('scroll', closeMsgMenu, true);
    window.addEventListener('resize', closeMsgMenu, true);
  }, 0);
}

export function buildMessageNode(m, opts) {
  opts = opts || {};
  const div = document.createElement('div');
  div.className = 'msg';
  div.dataset.id = m.id;

  div.appendChild(avatarEl(m.sender));
  const main = document.createElement('div');
  main.className = 'msg-main';
  div.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('b');
  name.textContent = m.sender.display_name;
  meta.appendChild(name);
  if (m.sender.username === 'webhook') {
    const tag = document.createElement('span');
    tag.className = 'bot-tag';
    tag.textContent = 'APP';
    tag.title = 'Posted by an incoming webhook';
    meta.appendChild(tag);
  }
  const when = document.createElement('span');
  when.textContent = relTime(m.created_at) + (m.edited_at ? ' (edited)' : '');
  when.title = new Date(m.created_at).toLocaleString();
  meta.appendChild(when);
  if (m.pinned) {
    const pinTag = document.createElement('span');
    pinTag.className = 'pin-tag';
    pinTag.textContent = '📌';
    pinTag.title = 'Pinned';
    meta.appendChild(pinTag);
  }

  // One source of truth for the per-message actions: rendered as inline hover
  // icons on desktop, and collapsed into a long-press menu on mobile (so they
  // never wrap onto a second row on a narrow screen).
  const acts = [
    { icon: 'reply', label: opts.inThread ? 'Reply in this thread' : 'Reply in thread', fn: () => openThread(m) },
  ];
  if (m.content) acts.push({ icon: 'copy', label: 'Copy text', fn: () => copyMessageText(m) });
  acts.push({ icon: 'pin', label: m.pinned ? 'Unpin' : 'Pin', cls: m.pinned ? 'active-pin' : '', fn: () => togglePin(m) });
  acts.push({ icon: 'clock', label: 'Remind me about this', fn: () => remindAboutMessage(m) });
  if (state.me && m.sender.id === state.me.id) {
    if (!isTaskLine(m.content)) {
      acts.push({ icon: 'checklist', label: 'Convert to task', fn: () => convertToTask(m) });
    }
    acts.push({ icon: 'pencil', label: 'Edit', fn: () => editMessage(m) });
    acts.push({ icon: 'trash', label: 'Delete', fn: () => deleteMessage(m) });
  } else if (m.sender.username === 'webhook' && state.currentChannel && canManage(state.currentChannel)) {
    acts.push({ icon: 'trash', label: 'Delete', fn: () => deleteMessage(m) });
  }
  const actions = document.createElement('span');
  actions.className = 'actions';
  for (const a of acts) {
    const b = document.createElement('button');
    b.innerHTML = svgIcon(a.icon, 'act-ico');
    b.title = a.label;
    if (a.cls) b.classList.add(a.cls);
    b.onclick = a.fn;
    actions.appendChild(b);
  }
  meta.appendChild(actions);

  // Touch/narrow clients open the action menu with a LONG-PRESS (like iOS),
  // not a tap — so the tap that dismisses one menu can never pop another. The
  // menu leads with a quick-reaction emoji row. Only true wide hover-capable
  // desktops skip it and use the inline hover icons; an iPad PWA is wide but
  // has no hover, so it gets the long-press menu too.
  let lpTimer = null, lpX = 0, lpY = 0, lpFired = false;
  const lpCancel = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
  div.addEventListener('pointerdown', (e) => {
    if (!isNarrow() && hasHover()) return;
    if (e.target.closest('a, button, input, label, img, .reply-preview, .thread-summary')) return;
    lpX = e.clientX; lpY = e.clientY; lpFired = false;
    lpTimer = setTimeout(() => {
      lpTimer = null; lpFired = true;
      if (navigator.vibrate) navigator.vibrate(10);  // little haptic tick where supported
      openMsgMenu(div, acts, m);
    }, 450);
  });
  // Any movement (scrolling), release, or cancel before the timer = not a long-press.
  div.addEventListener('pointermove', (e) => {
    if (lpTimer && Math.hypot(e.clientX - lpX, e.clientY - lpY) > 10) lpCancel();
  });
  div.addEventListener('pointerup', lpCancel);
  div.addEventListener('pointercancel', lpCancel);
  // Swallow the click that trails a completed long-press so it can't hit a link.
  div.addEventListener('click', (e) => {
    if (lpFired) { lpFired = false; e.preventDefault(); e.stopPropagation(); }
  }, true);
  // Keep the browser's own long-press context menu out of the way on touch.
  div.addEventListener('contextmenu', (e) => { if (!hasHover()) e.preventDefault(); });
  main.appendChild(meta);

  if (m.reply_to) {
    const rp = document.createElement('div');
    rp.className = 'reply-preview';
    rp.title = 'Go to replied message';
    const who = document.createElement('b');
    who.textContent = '↳ ' + m.reply_to.sender_name;
    const snip = document.createElement('span');
    snip.className = 'snip';
    snip.textContent = m.reply_to.snippet;
    rp.appendChild(who);
    rp.appendChild(snip);
    rp.onclick = () => jumpToMessage(m.channel_id, m.reply_to.id);
    main.appendChild(rp);
  }

  if (m.content) {
    main.appendChild(renderBody(m.content, m.task_cleared, (line, checked) => toggleTask(m.id, line, checked)));
  }

  if (m.file) {
    if (m.file.content_type.startsWith('image/')) {
      const img = document.createElement('img');
      img.className = 'attachment-img';
      img.alt = m.file.filename;
      img.title = 'Click to view full size';
      img.onclick = () => openViewer(m.file);
      // An image loads with zero height, then grows — re-pin to the bottom as it
      // lands so a channel that ends in an image still opens fully scrolled down.
      img.addEventListener('load', keepMessagesPinned);
      loadAuthedBlob(m.file.id).then(url => { img.src = url; }).catch(() => img.remove());
      main.appendChild(img);
    } else {
      const row = document.createElement('div');
      row.className = 'attachment-row';
      const a = document.createElement('a');
      a.href = '#';
      a.className = 'attachment-file';
      const viewable = previewKind(m.file.content_type) !== null;
      a.textContent = `📄 ${m.file.filename} (${fmtSize(m.file.size_bytes)})`;
      a.title = viewable ? 'Click to preview' : 'Click to download';
      a.onclick = e => { e.preventDefault(); openViewer(m.file); };
      row.appendChild(a);
      const dl = document.createElement('button');
      dl.textContent = '⤓';
      dl.title = 'Download';
      dl.onclick = () => downloadFile(m.file);
      row.appendChild(dl);
      main.appendChild(row);
    }
  }

  if (m.link_previews && m.link_previews.length) {
    main.appendChild(linkCardsEl(m.link_previews));
  }

  const rrow = document.createElement('div');
  rrow.className = 'reactions';
  renderReactions(rrow, m.id, m.reactions);
  main.appendChild(rrow);

  if (!opts.inThread && m.reply_count > 0) {
    main.appendChild(threadSummaryEl(m));
  }

  return div;
}

export function linkCardsEl(previews) {
  const wrap = document.createElement('div');
  wrap.className = 'link-cards';
  for (const p of previews) {
    const a = document.createElement('a');
    a.className = 'link-card';
    a.href = p.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    if (p.site_name) {
      const site = document.createElement('div');
      site.className = 'lc-site';
      site.textContent = p.site_name;
      a.appendChild(site);
    }
    if (p.title) {
      const t = document.createElement('div');
      t.className = 'lc-title';
      t.textContent = p.title;
      a.appendChild(t);
    }
    if (p.description) {
      const d = document.createElement('div');
      d.className = 'lc-desc';
      d.textContent = p.description;
      a.appendChild(d);
    }
    wrap.appendChild(a);
  }
  return wrap;
}

export function renderMessage(m) {
  $('messages').appendChild(buildMessageNode(m));
}

export async function sendMessage(e) {
  e.preventDefault();
  const content = serializeEditor();
  if (!content || !state.currentChannel) return;
  if (content.length > 4000) { appAlert('Message is too long (4000 characters max)'); return; }
  if (editingMessageId) {
    const id = editingMessageId;
    exitEditMode();
    try {
      await api(`/messages/${id}`, { method: 'PATCH', body: JSON.stringify({ content }) });
    } catch (err) { appAlert(err.message); }
    return;
  }
  clearEditor();
  clearDraft(state.currentChannel.id);
  try {
    await api(`/channels/${state.currentChannel.id}/messages`, {
      method: 'POST', body: JSON.stringify({ content }),
    });
  } catch (err) { $('send-input').textContent = content; appAlert(err.message); }
}

async function togglePin(m) {
  try {
    await api(`/messages/${m.id}/pin`, { method: m.pinned ? 'DELETE' : 'POST' });
  } catch (e) { appAlert(e.message); }
}

async function convertToTask(m) {
  const lines = m.content.split('\n');
  if (isTaskLine(lines[0])) return;
  lines[0] = OPEN_TASK_PREFIX + lines[0];
  try {
    await api(`/messages/${m.id}`, {
      method: 'PATCH', body: JSON.stringify({ content: lines.join('\n') }),
    });
  } catch (e) { appAlert(e.message); }
}

async function deleteMessage(m) {
  if (!await appConfirm('Delete this message?')) return;
  try { await api(`/messages/${m.id}`, { method: 'DELETE' }); }
  catch (err) { appAlert(err.message); }
}

export async function uploadAttachment() {
  const file = $('file-input').files[0];
  $('file-input').value = '';
  if (!file || !state.currentChannel) return;
  const attach = $('attach-btn');
  attach.disabled = true;
  try {
    const fd = new FormData();
    fd.append('upload', file);
    const meta = await api(`/channels/${state.currentChannel.id}/files`, { method: 'POST', body: fd });
    await api(`/channels/${state.currentChannel.id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content: serializeEditor(), file_id: meta.id }),
    });
    clearEditor();
    clearDraft(state.currentChannel.id);
  } catch (err) { appAlert(err.message); }
  finally { attach.disabled = false; }
}

export function renderReactions(container, messageId, reactions) {
  container.innerHTML = '';
  for (const r of reactions || []) {
    const pill = document.createElement('button');
    pill.className = 'reaction' + (state.me && r.user_ids.includes(state.me.id) ? ' mine' : '');
    pill.textContent = `${r.emoji} ${r.count}`;
    pill.title = state.me && r.user_ids.includes(state.me.id) ? 'Click to remove your reaction' : 'Click to react too';
    pill.onclick = () => toggleReaction(messageId, r.emoji);
    container.appendChild(pill);
  }
  // "+" reveals an inline emoji strip: hover on desktop, tap on touch. The 40
  // buttons are built lazily on first hover/tap — building them eagerly cost
  // ~4,000 hidden nodes per 100-message channel load.
  const zone = document.createElement('span');
  zone.className = 'react-zone';
  const add = document.createElement('button');
  add.className = 'reaction reaction-add';
  add.textContent = '+';
  add.title = 'Add reaction';
  const strip = document.createElement('span');
  strip.className = 'emoji-strip';
  const buildStrip = () => {
    if (strip.childElementCount) return;
    for (const emoji of meta.emoji) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = emoji;
      b.onclick = e => {
        e.stopPropagation();
        zone.classList.remove('open');
        toggleReaction(messageId, emoji);
      };
      strip.appendChild(b);
    }
  };
  zone.addEventListener('pointerenter', buildStrip);
  add.onclick = e => { e.stopPropagation(); buildStrip(); zone.classList.toggle('open'); };
  zone.appendChild(add);
  zone.appendChild(strip);
  container.appendChild(zone);
}

async function toggleReaction(messageId, emoji) {
  try {
    const reactions = await api(`/messages/${messageId}/reactions`, {
      method: 'POST', body: JSON.stringify({ emoji }),
    });
    const row = document.querySelector(`.msg[data-id="${messageId}"] .reactions`);
    if (row) renderReactions(row, messageId, reactions);
  } catch (e) { appAlert(e.message); }
}

// Tick or clear one task line of a message. The server broadcasts the updated
// message, which re-renders it everywhere; this only reports a failure.
export async function toggleTask(messageId, line, checked) {
  try {
    await api(`/messages/${messageId}/checkbox`, {
      method: 'POST', body: JSON.stringify({ line, checked }),
    });
    refreshTaskCount();
  } catch (err) {
    appAlert(err.message);
    throw err;
  }
}
