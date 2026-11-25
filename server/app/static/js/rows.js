// How one item looks in a list. Each kind of row piece is defined here once
// and used by every list that shows it (the channel pane, the cross-channel
// inboxes, the timeline), so the same thing never drifts into two renderings.
import { api } from './api.js';
import { avatarEl } from './avatars.js';
import { appAlert } from './dialogs.js';
import { el } from './dom.js';
import { plural, relTime } from './format.js';
import { toggleTask } from './messages.js';

export function replyCount(n) {
  return plural(n, 'reply', 'replies');
}

// The few people who replied, as overlapping avatars; null when nobody has.
export function facesEl(users) {
  if (!users || !users.length) return null;
  return el('span', { className: 'faces' }, users.map(avatarEl));
}

// What a message is "about" in one line: its text, else its attachment's name.
function messageSnippet(m, max) {
  return m.content ? m.content.slice(0, max) : (m.file ? m.file.filename : '(message)');
}

// "Sender: first words of the message".
export function senderSnippetEl(m, max) {
  return el('div', null, el('b', null, m.sender.display_name + ': '), messageSnippet(m, max));
}

// Faces plus "3 replies · 5m ago" for a thread's root message.
export function threadActivityEl(root, className) {
  const when = root.thread_last_at ? ' · ' + relTime(root.thread_last_at) : '';
  return el('div', { className },
    facesEl(root.thread_repliers),
    el('span', null, replyCount(root.reply_count) + when));
}

// A checkbox that marks an open task done. `onDone` runs after the server
// accepted it; on failure the box clears and the user is told (by toggleTask).
export function taskCheckbox(task, onDone) {
  const cb = el('input', { type: 'checkbox', title: 'Mark done' });
  cb.onchange = async () => {
    cb.disabled = true;
    try {
      await toggleTask(task.message_id, task.line, true);
      onDone();
    } catch {
      cb.checked = false;
      cb.disabled = false;
    }
  };
  return cb;
}

// An "Unpin" button for a pinned message. `onDone` runs once it is unpinned.
export function unpinButton(message, onDone) {
  const btn = el('button', null, 'Unpin');
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/messages/${message.id}/pin`, { method: 'DELETE' });
      if (onDone) onDone();
    } catch (e) {
      btn.disabled = false;
      appAlert(e.message);
    }
  };
  return btn;
}
