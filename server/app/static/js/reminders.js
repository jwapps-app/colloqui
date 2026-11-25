import { api } from './api.js';
import { appAlert, appPrompt } from './dialogs.js';
import { $, el, emptyRow, titledRow } from './dom.js';
import { loadInfoPane } from './infopane.js';
import { meta, recurrenceLabel } from './meta.js';
import { state } from './state.js';

// Promise-based "when?" picker: quick options + native datetime control.
let whenResolve = null;

// Resolves to { due: Date, recurrence: string|null } or null if cancelled.
// `prefill` ({ due, recurrence }) pre-populates the picker when editing.
function pickWhen(prefill) {
  return new Promise(resolve => {
    whenResolve = resolve;
    const p = n => String(n).padStart(2, '0');
    let base;
    if (prefill && prefill.due) {
      base = new Date(prefill.due);
    } else {
      base = new Date(Date.now() + 3600000);
      base.setMinutes(Math.ceil(base.getMinutes() / 15) * 15, 0, 0);
      // Keep the default within the 8 AM–6 PM window the dropdown offers.
      if (base.getHours() < 8) {
        base.setHours(8, 0, 0, 0);
      } else if (base.getHours() > 18) {
        base.setDate(base.getDate() + 1);
        base.setHours(8, 0, 0, 0);
      }
    }
    const now = new Date();
    $('when-date').min = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
    $('when-date').value = `${base.getFullYear()}-${p(base.getMonth() + 1)}-${p(base.getDate())}`;
    $('when-hour').value = String(base.getHours());
    $('when-min').value = String(Math.round(base.getMinutes() / 15) * 15 % 60);
    $('when-repeat').value = (prefill && prefill.recurrence) || '';
    $('when').classList.remove('hidden');
  });
}

export function closeWhen(result) {
  $('when').classList.add('hidden');
  if (whenResolve) {
    whenResolve(result ? { due: result, recurrence: $('when-repeat').value || null } : null);
    whenResolve = null;
  }
}

async function createReminder(text, due, messageId, recurrence) {
  await api('/reminders', {
    method: 'POST',
    body: JSON.stringify({
      text,
      due_at: due.toISOString(),
      message_id: messageId || null,
      channel_id: !messageId && state.currentChannel ? state.currentChannel.id : null,
      recurrence: recurrence || null,
    }),
  });
  if (!$('info-pane').classList.contains('hidden')) loadInfoPane();
}

export async function remindAboutMessage(m) {
  const r = await pickWhen();
  if (!r) return;
  try {
    const snippet = m.content ? m.content.slice(0, 120) : (m.file ? m.file.filename : 'message');
    await createReminder(`${m.sender.display_name}: "${snippet}"`, r.due, m.id, r.recurrence);
    appAlert(`Reminder set for ${r.due.toLocaleString()}${r.recurrence ? ', repeating ' + r.recurrence : ''} ✓`);
  } catch (e) { appAlert(e.message); }
}

export async function addReminderFromPanel() {
  const text = $('rem-text').value.trim();
  if (!text) { $('rem-text').focus(); return; }
  const r = await pickWhen();
  if (!r) return;
  try {
    await createReminder(text, r.due, null, r.recurrence);
    $('rem-text').value = '';
    await loadReminders();
  } catch (e) { appAlert(e.message); }
}

async function editReminder(rem) {
  const text = await appPrompt('Reminder text:', { value: rem.text, okText: 'Next' });
  if (text === null || !text.trim()) return;
  const r = await pickWhen({ due: new Date(rem.due_at), recurrence: rem.recurrence });
  if (!r) return;
  try {
    await api(`/reminders/${rem.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        text: text.trim(),
        due_at: r.due.toISOString(),
        recurrence: r.recurrence || '',  // "" clears recurrence
      }),
    });
    await loadReminders();
  } catch (e) { appAlert(e.message); }
}

export async function loadReminders() {
  const list = $('rem-list');
  const reminders = await api('/reminders');
  list.innerHTML = '';
  if (reminders.length === 0) {
    list.appendChild(emptyRow('No pending reminders.'));
  }
  for (const r of reminders) {
    const li = titledRow(
      r.text,
      'due ' + new Date(r.due_at).toLocaleString()
      + (r.recurrence ? ' · repeats ' + r.recurrence : ''));
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => editReminder(r);
    li.appendChild(edit);
    const btn = document.createElement('button');
    btn.textContent = 'Cancel';
    btn.onclick = async () => {
      try { await api(`/reminders/${r.id}`, { method: 'DELETE' }); await loadReminders(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

export function submitCustomWhen() {
  const date = $('when-date').value;
  if (!date) return;
  const h = String(+$('when-hour').value).padStart(2, '0');
  const m = String(+$('when-min').value).padStart(2, '0');
  const d = new Date(`${date}T${h}:${m}`);
  if (isNaN(d) || d <= new Date()) { appAlert('Pick a time in the future'); return; }
  closeWhen(d);
}

// The "Repeat" choices in the when-picker, from the server's list.
export function renderRecurrenceOptions() {
  const sel = $('when-repeat');
  sel.innerHTML = '';
  sel.appendChild(el('option', { value: '' }, "Doesn't repeat"));
  for (const r of meta.recurrences) sel.appendChild(el('option', { value: r }, recurrenceLabel(r)));
}
