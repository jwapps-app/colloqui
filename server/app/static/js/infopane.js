import { api } from './api.js';
import { canManage, channelById, jumpToMessage, loadChannels, renderChannels } from './channels.js';
import { appAlert, appChoose, appConfirm, appPrompt, guarded } from './dialogs.js';
import { $, el, emptyRow, fillList, isNarrow } from './dom.js';
import { closePreview } from './files.js';
import { relTime } from './format.js';
import { renderInline } from './markdown.js';
import { meta, notifyWording } from './meta.js';
import { senderSnippetEl, taskCheckbox, threadActivityEl, unpinButton } from './rows.js';
import { state } from './state.js';
import { closeThread, openThread } from './threads.js';

export function closeInfoPane() {
  $('info-pane').classList.add('hidden');
}

// After closing a thread/preview on desktop, fall back to the Channel pane
// (the default right-hand column). No-op on mobile or with no channel open.
export function maybeReopenInfoPane() {
  if (!isNarrow() && state.currentChannel) {
    $('info-pane').classList.remove('hidden');
    loadInfoPane();
  }
}

// When jumping to a message from the pane, only close the pane on mobile
// (where it's a full-screen overlay covering the chat). On desktop it's a
// side column, so keep it open.
function jumpFromInfoPane(channelId, messageId) {
  if (isNarrow()) closeInfoPane();
  jumpToMessage(channelId, messageId);
}

export async function toggleInfoPane() {
  if (!$('info-pane').classList.contains('hidden')) { closeInfoPane(); return; }
  if (!state.currentChannel) return;
  closePreview();
  closeThread();
  $('info-pane').classList.remove('hidden');
  await loadInfoPane();
}

let infoPaneTimer = null;
export function scheduleInfoPane() {
  if ($('info-pane').classList.contains('hidden')) return;
  clearTimeout(infoPaneTimer);
  infoPaneTimer = setTimeout(loadInfoPane, 120);
}

function renderNotifyPref() {
  const box = $('notify-pref');
  if (!box || !state.currentChannel) return;
  box.innerHTML = '';
  const current = state.currentChannel.notify_level || meta.defaultNotifyLevel;
  for (const value of meta.notifyLevels) {
    const [label, hint] = notifyWording(value);
    const b = document.createElement('button');
    b.textContent = label;
    b.title = hint;
    if (value === current) b.classList.add('active');
    b.onclick = () => setNotifyLevel(value);
    box.appendChild(b);
  }
}

async function setNotifyLevel(level) {
  if (!state.currentChannel || state.currentChannel.notify_level === level) return;
  try {
    const updated = await api(`/channels/${state.currentChannel.id}/notify`, {
      method: 'PUT', body: JSON.stringify({ level }),
    });
    state.currentChannel.notify_level = updated.notify_level;
    const ch = channelById(updated.id);
    if (ch) ch.notify_level = updated.notify_level;
    renderNotifyPref();
    renderChannels();
  } catch (e) { appAlert(e.message); }
}

async function renderWebhooks() {
  const section = $('info-webhooks-section');
  if (!state.currentChannel || !canManage(state.currentChannel)) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  const cid = state.currentChannel.id;
  const list = $('info-webhooks');
  let hooks;
  try { hooks = await api(`/channels/${cid}/webhooks`); }
  catch { return; }
  if (!state.currentChannel || state.currentChannel.id !== cid) return;
  list.innerHTML = '';
  if (!hooks.length) {
    list.appendChild(emptyRow('No webhooks. Use + to create one.', 'empty'));
    return;
  }
  for (const h of hooks) {
    const li = document.createElement('li');
    const grow = document.createElement('div');
    grow.className = 'grow';
    const b = document.createElement('b');
    b.textContent = h.name;
    grow.appendChild(b);
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = h.last_used_at ? 'last used ' + relTime(h.last_used_at) : 'never used';
    grow.appendChild(sub);
    li.appendChild(grow);
    const del = document.createElement('button');
    del.textContent = 'Delete';
    del.onclick = async () => {
      if (!await appConfirm(`Delete webhook “${h.name}”? Its URL will stop working.`)) return;
      try { await api(`/webhooks/${h.id}`, { method: 'DELETE' }); renderWebhooks(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(del);
    list.appendChild(li);
  }
}

export async function createWebhook() {
  if (!state.currentChannel) return;
  const name = await appPrompt('Name this webhook (shown as the sender), e.g. “Backups”:');
  if (!name || !name.trim()) return;
  try {
    const hook = await api(`/channels/${state.currentChannel.id}/webhooks`, {
      method: 'POST', body: JSON.stringify({ name: name.trim() }),
    });
    let copied = false;
    try { await navigator.clipboard.writeText(hook.url); copied = true; } catch {}
    await appAlert(
      `Webhook “${hook.name}” created.\n\nPOST JSON like {"text":"hello"} to:\n\n${hook.url}\n\n`
      + (copied ? 'The URL has been copied to your clipboard. ' : '')
      + 'This is the only time the URL is shown — save it now.');
    renderWebhooks();
  } catch (e) { appAlert(e.message); }
}

// Manager-only "Space" section in the info pane: shows the current space and a
// direct "move to another space" action (no rename detour).
function renderChannelSpace() {
  const section = $('info-space-section');
  if (!state.currentChannel || !canManage(state.currentChannel)) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  const sp = state.spaces.find(s => s.id === state.currentChannel.space_id);
  $('info-space-current').textContent = 'Currently in: ' + (sp ? sp.name : '—');
  const btn = $('channel-move-btn');
  btn.disabled = state.spaces.filter(s => s.id !== state.currentChannel.space_id).length === 0;
  btn.onclick = moveChannelPrompt;
}

async function moveChannelPrompt() {
  if (!state.currentChannel || !canManage(state.currentChannel)) return;
  const others = state.spaces.filter(s => s.id !== state.currentChannel.space_id);
  if (!others.length) { appAlert('There are no other spaces to move this into.'); return; }
  const choice = await appChoose(
    `Move “#${state.currentChannel.name}” to which space?`,
    others.map(s => ({ value: s.id, label: s.name })),
    { okText: 'Move' },
  );
  if (!choice) return;
  try {
    await api(`/channels/${state.currentChannel.id}/space`, {
      method: 'PUT', body: JSON.stringify({ space_id: choice }),
    });
    await loadChannels();
    loadInfoPane();
  } catch (e) { appAlert(e.message); }
}

export async function loadInfoPane() {
  if (!state.currentChannel || $('info-pane').classList.contains('hidden')) return;
  renderNotifyPref();
  renderChannelSpace();
  renderWebhooks();
  const cid = state.currentChannel.id;
  // Fetch BEFORE clearing so the list never flashes empty mid-refresh.
  let tasks = [], reminders = [], pins = [], threads = [];
  try {
    [tasks, reminders, pins, threads] = await Promise.all([
      api('/tasks'), api('/reminders'), api(`/channels/${cid}/pins`),
      api(`/channels/${cid}/threads`),
    ]);
  } catch (e) { appAlert(e.message); return; }
  if (!state.currentChannel || state.currentChannel.id !== cid) return;

  fillList($('info-threads'), threads, {
    empty: 'No threads in this channel.', emptyClass: 'empty',
    row: root => el('li', null,
      el('div', { className: 'grow', onclick: () => openThread(root) },
        senderSnippetEl(root, 80),
        threadActivityEl(root, 'sub thread-sub'))),
  });

  fillList($('info-pins'), pins, {
    empty: 'No pinned messages.', emptyClass: 'empty',
    row: pinned => el('li', null,
      el('div', { className: 'grow', onclick: () => jumpFromInfoPane(cid, pinned.id) },
        senderSnippetEl(pinned, 100)),
      unpinButton(pinned)),  // the pins.changed event then refreshes this pane
  });

  fillList($('info-tasks'), tasks.filter(t => t.channel_id === cid), {
    empty: 'No open tasks in this channel.', emptyClass: 'empty',
    row: task => {
      const text = el('div', { className: 'grow', onclick: () => jumpFromInfoPane(cid, task.message_id) });
      renderInline(task.text, text);
      const li = el('li', null);
      li.append(taskCheckbox(task, () => li.remove()), text);
      return li;
    },
  });

  const mine = reminders
    .filter(r => r.channel_id === cid)
    .sort((a, b) => new Date(a.due_at) - new Date(b.due_at));
  fillList($('info-reminders'), mine, {
    empty: 'No reminders for this channel.', emptyClass: 'empty',
    row: r => el('li', null,
      el('div', {
        className: 'grow',
        onclick: r.message_id ? () => jumpFromInfoPane(cid, r.message_id) : null,
      }, r.text, el('div', { className: 'sub' }, 'due ' + new Date(r.due_at).toLocaleString())),
      el('button', {
        onclick: () => guarded(async () => {
          await api(`/reminders/${r.id}`, { method: 'DELETE' });
          await loadInfoPane();
        }),
      }, 'Cancel')),
  });
}
