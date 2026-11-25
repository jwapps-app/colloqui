import { api } from './api.js';
import {
  closeMention,
  editingMessageId,
  exitEditMode,
  restoreDraft,
  setComposerEnabled,
} from './composer.js';
import { appAlert, appConfirm, appPrompt } from './dialogs.js';
import { $, isNarrow, keyboardActivatable } from './dom.js';
import { closePreview } from './files.js';
import { svgIcon } from './icons.js';
import { closeInfoPane, loadInfoPane } from './infopane.js';
import { buildMessageNode, renderMessage } from './messages.js';
import { clearChannelNotifs } from './notifications.js';
import { renderTyping, typers } from './realtime.js';
import { lastSeenText, presenceOf, state } from './state.js';
import { closeThread, openThread } from './threads.js';
import { scrollMessagesToBottom, syncViewportHeight } from './viewport.js';

let oldestMessageId = null;
let allHistoryLoaded = false;
let loadingOlder = false;

function channelLabel(ch) {
  if (!ch.is_dm) return '# ' + ch.name;
  if (ch.dm_user) return ch.dm_user.display_name;
  if (ch.dm_members && ch.dm_members.length) {
    return ch.dm_members.map(u => u.display_name).join(', ');
  }
  return 'DM';
}

export function canManage(ch) {
  return !ch.is_dm && (ch.my_role === 'owner' || (state.me && state.me.is_admin));
}

function channelLi(ch) {
  const li = document.createElement('li');
  li.dataset.id = ch.id;
  const muted = ch.notify_level === 'muted';
  if (muted) li.classList.add('muted');
  if (ch.is_dm && ch.dm_user) {
    const dot = document.createElement('span');
    const st = presenceOf(ch.dm_user.id);
    dot.className = 'dot ' + st;
    dot.dataset.uid = ch.dm_user.id;  // so presence updates can patch it in place
    dot.title = ch.dm_user.status || (st === 'offline' ? lastSeenText(ch.dm_user) : st);
    li.appendChild(dot);
  }
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = channelLabel(ch);
  li.appendChild(label);
  if (ch.open_task_count > 0) {
    const tasks = document.createElement('span');
    tasks.className = 'task-chip';
    tasks.textContent = '☑ ' + ch.open_task_count;
    tasks.title = `${ch.open_task_count} open task(s)`;
    li.appendChild(tasks);
  }
  if (ch.recent_count) {
    const recent = document.createElement('span');
    recent.className = 'msg-count';
    recent.textContent = ch.recent_count;
    recent.title = `${ch.recent_count} message(s) in the last 7 days`;
    li.appendChild(recent);
  }
  if (muted) {
    const bell = document.createElement('span');
    bell.className = 'mute-tag';
    bell.textContent = '🔕';
    bell.title = 'Muted';
    li.appendChild(bell);
  } else if (ch.unread_count > 0) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = ch.unread_count > 99 ? '99+' : ch.unread_count;
    badge.title = `${ch.unread_count} unread`;
    li.appendChild(badge);
  }
  if (state.currentChannel && state.currentChannel.id === ch.id) li.classList.add('active');
  li.onclick = () => selectChannel(ch);
  keyboardActivatable(li, () => selectChannel(ch));
  return li;
}

export function channelById(id) {
  return state.channels.find(c => c.id === id);
}

export function markRead(channelId) {
  api(`/channels/${channelId}/read`, { method: 'POST' }).catch(() => {});
}

// Jump to a channel from a notification (service-worker message or ?channel=).
export function openChannelFromLink(channelId, rootId) {
  const ch = channelById(channelId);
  if (!ch) return;
  $('notifs').classList.add('hidden');
  selectChannel(ch).then(() => { if (rootId) openThread({ id: rootId }); });
}

// Collapsed spaces persist per device (a view preference, not server state).
let collapsedSpaces = new Set(JSON.parse(localStorage.getItem('collapsedSpaces') || '[]'));

function toggleSpaceCollapsed(spaceId) {
  if (collapsedSpaces.has(spaceId)) collapsedSpaces.delete(spaceId);
  else collapsedSpaces.add(spaceId);
  localStorage.setItem('collapsedSpaces', JSON.stringify([...collapsedSpaces]));
  renderChannels();
}

export function renderChannels() {
  const container = $('spaces-container');
  const dmList = $('dm-list');
  container.innerHTML = ''; dmList.innerHTML = '';

  const bySpace = new Map();
  for (const ch of state.channels) {
    if (ch.is_dm) { dmList.appendChild(channelLi(ch)); continue; }
    if (!bySpace.has(ch.space_id)) bySpace.set(ch.space_id, []);
    bySpace.get(ch.space_id).push(ch);
  }

  for (const sp of state.spaces) {
    const inSpace = bySpace.get(sp.id) || [];
    const collapsed = collapsedSpaces.has(sp.id);
    const section = document.createElement('div');
    section.className = 'section' + (collapsed ? ' collapsed' : '');
    const h3 = document.createElement('h3');
    // Chevron + name toggle the collapse; the counts and "+" stay separate.
    const toggle = document.createElement('span');
    toggle.className = 'space-toggle';
    toggle.title = collapsed ? 'Expand space' : 'Collapse space';
    toggle.insertAdjacentHTML('afterbegin', svgIcon('chevron', 'space-chevron'));
    const name = document.createElement('span');
    name.className = 'space-name';
    name.textContent = sp.name;
    toggle.appendChild(name);
    toggle.onclick = () => toggleSpaceCollapsed(sp.id);
    keyboardActivatable(toggle, () => toggleSpaceCollapsed(sp.id));
    toggle.setAttribute('aria-expanded', String(!collapsed));
    h3.appendChild(toggle);
    const btns = document.createElement('span');
    btns.className = 'space-side';
    if (collapsed) {
      // Rolled-up totals across the space's channels, shown the same way the
      // channel rows show theirs. Muted channels keep their unreads to themselves.
      const tasks = inSpace.reduce((n, c) => n + (c.open_task_count || 0), 0);
      const recent = inSpace.reduce((n, c) => n + (c.recent_count || 0), 0);
      const unread = inSpace.reduce(
        (n, c) => n + (c.notify_level === 'muted' ? 0 : (c.unread_count || 0)), 0);
      if (tasks > 0) {
        const chip = document.createElement('span');
        chip.className = 'task-chip';
        chip.textContent = '☑ ' + tasks;
        chip.title = `${tasks} open task(s) in this space`;
        btns.appendChild(chip);
      }
      if (recent > 0) {
        const count = document.createElement('span');
        count.className = 'msg-count';
        count.textContent = recent;
        count.title = `${recent} message(s) in the last 7 days`;
        btns.appendChild(count);
      }
      if (unread > 0) {
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = unread > 99 ? '99+' : unread;
        badge.title = `${unread} unread`;
        btns.appendChild(badge);
      }
    }
    // Space management (rename / add members) lives in Settings → Spaces now,
    // so the sidebar header only carries the "add channel" action.
    const add = document.createElement('button');
    add.textContent = '+';
    add.title = 'Create or join a channel in this space';
    add.onclick = () => newChannel(sp.id);
    btns.appendChild(add);
    h3.appendChild(btns);
    section.appendChild(h3);
    if (!collapsed) {
      const ul = document.createElement('ul');
      for (const ch of inSpace) ul.appendChild(channelLi(ch));
      section.appendChild(ul);
    }
    container.appendChild(section);
  }
  updateActionIcons();
}

// Gray out the pins header icon when nothing is pinned anywhere. (Threads is
// driven by refreshThreadCount to match its inbox; tasks/notifs toggle their
// own icons elsewhere.)
function updateActionIcons() {
  $('pins-btn').classList.toggle('active', state.channels.some(c => c.pinned_count > 0));
}

export async function loadChannels() {
  [state.spaces, state.channels] = await Promise.all([api('/spaces'), api('/channels')]);
  if (state.currentChannel) {
    const fresh = state.channels.find(c => c.id === state.currentChannel.id);
    if (fresh) { state.currentChannel = fresh; updateChannelHeader(); }
  }
  renderChannels();
}

function updateChannelHeader() {
  if (!state.currentChannel) {
    $('channel-title').textContent = 'Select a channel';
    $('channel-topic').textContent = '';
    $('channel-actions').classList.add('hidden');
    return;
  }
  $('channel-title').textContent = channelLabel(state.currentChannel);
  $('channel-topic').textContent = state.currentChannel.topic || '';
  $('channel-actions').classList.remove('hidden');
  $('members-btn').classList.toggle('hidden', state.currentChannel.is_dm);
  $('edit-channel-btn').classList.toggle('hidden', !canManage(state.currentChannel));
}

export async function selectChannel(ch) {
  exitEditMode();
  closeThread();
  closeMention();
  state.currentChannel = ch;
  if (ch.unread_count) { ch.unread_count = 0; markRead(ch.id); }
  clearChannelNotifs(ch.id);
  typers.clear(); renderTyping();
  closePreview();
  renderChannels();
  updateChannelHeader();
  $('app').classList.add('chat-open');
  setComposerEnabled(true);
  if (!editingMessageId) restoreDraft(ch.id);
  const messages = await api(`/channels/${ch.id}/messages?limit=100`);
  // A slower request for a channel you've since left must not paint its
  // history under the channel you're now looking at.
  if (!state.currentChannel || state.currentChannel.id !== ch.id) return;
  const box = $('messages');
  box.innerHTML = '';
  messages.forEach(renderMessage);
  state.stickBottom = true;
  scrollMessagesToBottom();
  syncViewportHeight();
  oldestMessageId = messages.length ? messages[0].id : null;
  allHistoryLoaded = messages.length < 100;
  loadingOlder = false;
  $('send-input').focus();
  // On desktop the Channel pane is the default right-hand column — always open.
  // On mobile it's a full-screen overlay, so leave it closed (open it manually).
  if (!isNarrow()) {
    closePreview();
    $('info-pane').classList.remove('hidden');
  } else {
    closeInfoPane();
  }
  loadInfoPane();  // refresh the info pane for the new channel
}

export async function maybeLoadOlder(force = false) {
  const box = $('messages');
  if ((!force && box.scrollTop > 80) || loadingOlder || allHistoryLoaded
      || !state.currentChannel || !oldestMessageId) return;
  loadingOlder = true;
  const channelAtStart = state.currentChannel.id;
  try {
    const older = await api(
      `/channels/${channelAtStart}/messages?before=${oldestMessageId}&limit=100`);
    if (!state.currentChannel || state.currentChannel.id !== channelAtStart) return;
    if (older.length < 100) allHistoryLoaded = true;
    if (older.length === 0) return;
    oldestMessageId = older[0].id;
    const fragment = document.createDocumentFragment();
    older.forEach(m => fragment.appendChild(buildMessageNode(m)));
    const prevHeight = box.scrollHeight;
    box.insertBefore(fragment, box.firstChild);
    box.scrollTop += box.scrollHeight - prevHeight;  // keep view anchored
  } catch {} finally {
    loadingOlder = false;
  }
}

export async function jumpToMessage(channelId, messageId) {
  const ch = state.channels.find(c => c.id === channelId);
  if (!ch) { appAlert('That conversation is no longer available'); return; }
  if (!state.currentChannel || state.currentChannel.id !== channelId) await selectChannel(ch);
  // Page back until the target message is in the DOM (or history runs out).
  let tries = 0;
  while (!document.querySelector(`.msg[data-id="${messageId}"]`)
         && !allHistoryLoaded && tries < 40) {
    await maybeLoadOlder(true);
    tries++;
  }
  const el = document.querySelector(`.msg[data-id="${messageId}"]`);
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.remove('flash');
    void el.offsetWidth;  // restart the animation if it's already applied
    el.classList.add('flash');
  }
}

export function clearChannelView() {
  exitEditMode();
  closeThread();
  state.currentChannel = null;
  $('messages').innerHTML = '';
  closePreview();
  closeInfoPane();
  setComposerEnabled(false);
  $('app').classList.remove('chat-open');
  updateChannelHeader();
  renderChannels();
}

export async function editChannel() {
  if (!state.currentChannel || !canManage(state.currentChannel)) return;
  const name = await appPrompt('Channel name:', { value: state.currentChannel.name || '' });
  if (name === null) return;
  const topic = await appPrompt('Topic (empty to clear):', { value: state.currentChannel.topic || '' });
  if (topic === null) return;
  try {
    await api(`/channels/${state.currentChannel.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: name.trim() || null, topic }),
    });
    await loadChannels();
  } catch (e) { appAlert(e.message); }
}

export async function openMembers() {
  if (!state.currentChannel || state.currentChannel.is_dm) return;
  $('members-title').textContent = `Members — ${channelLabel(state.currentChannel)}`;
  $('member-add-row').classList.toggle('hidden', !canManage(state.currentChannel));
  $('members').classList.remove('hidden');
  await loadMembers();
}

async function loadMembers() {
  const list = $('member-list');
  const members = await api(`/channels/${state.currentChannel.id}/members`);
  list.innerHTML = '';
  for (const m of members) {
    const li = document.createElement('li');
    const dot = document.createElement('span');
    dot.className = 'dot ' + presenceOf(m.id);
    dot.dataset.uid = m.id;
    dot.title = presenceOf(m.id) === 'offline' ? lastSeenText(m) : presenceOf(m.id);
    li.appendChild(dot);
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = m.display_name;
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = '@' + m.username + (m.is_admin ? ' · server admin' : '');
    grow.appendChild(sub);
    if (m.status) {
      const st = document.createElement('div');
      st.className = 'sub status-line';
      st.textContent = m.status;
      grow.appendChild(st);
    }
    li.appendChild(grow);
    if (canManage(state.currentChannel) && m.id !== state.me.id) {
      const btn = document.createElement('button');
      btn.textContent = 'Remove';
      btn.onclick = async () => {
        if (!await appConfirm(`Remove ${m.display_name} from this channel?`)) return;
        try {
          await api(`/channels/${state.currentChannel.id}/members/${m.id}`, { method: 'DELETE' });
          await loadMembers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(btn);
    }
    list.appendChild(li);
  }
}

export async function addMember() {
  const username = $('member-add-input').value.trim().toLowerCase();
  if (!username || !state.currentChannel) return;
  try {
    const user = (await api('/users')).find(u => u.username === username);
    if (!user) throw new Error('No such user');
    await api(`/channels/${state.currentChannel.id}/members`, {
      method: 'POST', body: JSON.stringify({ user_id: user.id }),
    });
    $('member-add-input').value = '';
    await loadMembers();
  } catch (e) { appAlert(e.message); }
}

async function newChannel(spaceId) {
  const name = await appPrompt('Channel name (joins it if it already exists):', { okText: 'Create' });
  if (!name) return;
  try {
    try {
      await api('/channels', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), space_id: spaceId }),
      });
    } catch (e) {
      const open = (await api(`/channels/browse?space_id=${spaceId}`))
        .find(c => c.name === name.trim());
      if (!open) throw e;
      await api(`/channels/${open.id}/join`, { method: 'POST' });
    }
    await loadChannels();
  } catch (e) { appAlert(e.message); }
}

export async function newDm() {
  const input = await appPrompt('Username(s) to message — comma-separate for a group:',
    { placeholder: 'alice  or  alice, bob', okText: 'Open DM' });
  if (!input) return;
  try {
    const wanted = input.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    if (!wanted.length) return;
    const all = await api('/users');
    const ids = [];
    for (const name of wanted) {
      const user = all.find(u => u.username === name);
      if (!user) throw new Error(`No such user: ${name}`);
      ids.push(user.id);
    }
    const ch = await api('/dms', { method: 'POST', body: JSON.stringify({ user_ids: ids }) });
    await loadChannels();
    selectChannel(state.channels.find(c => c.id === ch.id) || ch);
  } catch (e) { appAlert(e.message); }
}
