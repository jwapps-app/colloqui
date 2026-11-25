import { api } from './api.js';
import { channelById, jumpToMessage, renderChannels, selectChannel } from './channels.js';
import { setComposerEnabled } from './composer.js';
import { appAlert } from './dialogs.js';
import { $, el, emptyRow } from './dom.js';
import { relTime } from './format.js';
import { scheduleInfoPane } from './infopane.js';
import { renderInline } from './markdown.js';
import { openNotifs } from './notifications.js';
import { renderTyping, typers } from './realtime.js';
import { senderSnippetEl, taskCheckbox, threadActivityEl, unpinButton } from './rows.js';
import { state } from './state.js';
import { openThread } from './threads.js';

let taskCountTimer = null;
export async function refreshTaskCount() {
  try {
    const tasks = await api('/tasks');
    $('tasks-btn').classList.toggle('active', tasks.length > 0);
    // Update each channel's open-task chip in the sidebar.
    const counts = {};
    for (const t of tasks) counts[t.channel_id] = (counts[t.channel_id] || 0) + 1;
    let changed = false;
    for (const ch of state.channels) {
      const n = counts[ch.id] || 0;
      if (ch.open_task_count !== n) { ch.open_task_count = n; changed = true; }
    }
    if (changed) renderChannels();
    scheduleInfoPane();
  } catch {}
}
export function scheduleTaskCount() {
  clearTimeout(taskCountTimer);
  taskCountTimer = setTimeout(refreshTaskCount, 500);
}

// Threads icon mirrors the threads inbox: lit only when /threads would return
// something (threads you're in with activity in the last 7 days), so tapping a
// lit icon always shows content. (Channel-wide thread_count would light up for
// threads you're not part of.)
let threadCountTimer = null;
export async function refreshThreadCount() {
  try {
    const threads = await api('/threads');
    $('threads-btn').classList.toggle('active', threads.length > 0);
  } catch {}
}
export function scheduleThreadCount() {
  clearTimeout(threadCountTimer);
  threadCountTimer = setTimeout(refreshThreadCount, 500);
}

// Group items (tasks, pins) under a heading per channel, first-seen order.
function groupByChannel(items) {
  const groups = new Map();
  for (const it of items) {
    if (!groups.has(it.channel_id)) {
      groups.set(it.channel_id, {
        label: it.is_dm ? 'Direct message' : '# ' + it.channel_name, items: [],
      });
    }
    groups.get(it.channel_id).items.push(it);
  }
  return groups;
}

export async function openTasks() {
  $('tasks').classList.remove('hidden');
  await loadTasks();
}

async function loadTasks() {
  const list = $('task-list');
  list.innerHTML = '';
  let tasks;
  try { tasks = await api('/tasks'); }
  catch (e) { appAlert(e.message); return; }
  if (tasks.length === 0) { list.appendChild(emptyRow('No open tasks. 🎉')); return; }

  for (const [, group] of groupByChannel(tasks)) {
    list.appendChild(el('li', { className: 'task-group' }, group.label));
    for (const task of group.items) {
      const text = el('div', {
        className: 'task-text',
        title: 'Go to message',
        onclick: () => {
          $('tasks').classList.add('hidden');
          jumpToMessage(task.channel_id, task.message_id);
        },
      });
      renderInline(task.text, text);
      // Re-render on done: that also drops a group heading left with no tasks.
      list.appendChild(el('li', null,
        el('div', { className: 'task-item' }, taskCheckbox(task, loadTasks), text)));
    }
  }
}

export async function openPins() {
  $('pins').classList.remove('hidden');
  await loadPins();
}

async function loadPins() {
  const list = $('pins-list');
  list.innerHTML = '';
  let pins;
  try { pins = await api('/pins'); }
  catch (e) { appAlert(e.message); return; }
  if (!pins.length) { list.appendChild(emptyRow('No pinned messages.')); return; }
  for (const [, group] of groupByChannel(pins)) {
    list.appendChild(el('li', { className: 'task-group' }, group.label));
    for (const hit of group.items) {
      const m = hit.message;
      const where = senderSnippetEl(m, 120);
      where.className = 'grow';
      where.title = 'Go to message';
      where.onclick = () => { $('pins').classList.add('hidden'); jumpToMessage(hit.channel_id, m.id); };
      const li = el('li', null, where);
      li.appendChild(unpinButton(m, () => li.remove()));
      list.appendChild(li);
    }
  }
}

export async function openThreadsInbox() {
  $('threads-inbox').classList.remove('hidden');
  await loadThreadsInbox();
}

async function loadThreadsInbox() {
  const list = $('threads-list');
  list.innerHTML = '';
  let threads;
  try { threads = await api('/threads'); }
  catch (e) { appAlert(e.message); return; }
  if (!threads.length) {
    list.appendChild(emptyRow("No active threads. Use a message's reply action to start one."));
    return;
  }
  for (const t of threads) {
    const root = t.root;
    const snippet = el('div', { className: 'thread-digest-snippet' });
    renderInline(root.content ? root.content.slice(0, 120) : '(attachment)', snippet);
    list.appendChild(el('li', {
      className: 'thread-digest clickable',
      onclick: async () => {
        $('threads-inbox').classList.add('hidden');
        const ch = channelById(t.channel_id);
        if (!ch) return;
        await selectChannel(ch);
        openThread({ id: root.id });
      },
    },
    el('div', { className: 'thread-digest-where' },
      (t.is_dm ? '💬 ' : '# ') + (t.is_dm ? root.sender.display_name : t.channel_name)),
    snippet,
    threadActivityEl(root, 'thread-digest-sub')));
  }
}

export async function runSearch(e) {
  e.preventDefault();
  const q = $('search-input').value.trim();
  if (q.length < 2) return;
  try {
    const hits = await api(`/search/messages?q=${encodeURIComponent(q)}`);
    state.currentChannel = null;
    typers.clear(); renderTyping();
    renderChannels();
    $('app').classList.add('chat-open');
    $('channel-title').textContent = `Search: "${q}"`;
    $('channel-topic').textContent = hits.length ? `${hits.length} result(s)` : 'no results';
    $('channel-actions').classList.add('hidden');
    setComposerEnabled(false);
    const box = $('messages');
    box.innerHTML = '';
    for (const hit of hits) {
      const div = document.createElement('div');
      div.className = 'search-hit';
      const where = document.createElement('div');
      where.className = 'where';
      where.textContent = hit.is_dm ? 'Direct message' : '# ' + hit.channel_name;
      div.appendChild(where);
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${hit.message.sender.display_name} · ${relTime(hit.message.created_at)}`;
      div.appendChild(meta);
      const body = document.createElement('div');
      body.textContent = hit.message.content;
      div.appendChild(body);
      div.onclick = () => {
        const ch = state.channels.find(c => c.id === hit.channel_id);
        if (ch) selectChannel(ch);
      };
      box.appendChild(div);
    }
  } catch (err) { appAlert(err.message); }
}

// Switcher row in each list-pane header: hop straight to another list without
// closing first. Hide all four panes, then open the target (its opener loads it).
export const PANE_OPENERS = {
  'threads-inbox': openThreadsInbox,
  'pins': openPins,
  'tasks': openTasks,
  'notifs': openNotifs,
};
export function closeAllListPanes() {
  for (const id of ['notifs', 'tasks', 'threads-inbox', 'pins']) $(id).classList.add('hidden');
}
