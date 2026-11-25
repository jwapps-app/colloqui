// The closed sets of values the server defines (app/vocab.py): which reactions
// exist, which notification levels and reminder recurrences can be chosen. The
// client reads them from GET /meta after sign-in instead of carrying a copy,
// so adding one is a server-only change. Only the wording shown for each value
// lives here.
import { api } from './api.js';

export const meta = {
  emoji: [],
  notifyLevels: ['all', 'mentions', 'muted'],
  defaultNotifyLevel: 'all',
  recurrences: [],
};

const NOTIFY_WORDING = {
  all: ['All', 'Notify on every message'],
  mentions: ['Mentions', 'Only @mentions and threads you’re in'],
  muted: ['Muted', 'No notifications; hidden from unread'],
};

function titleCase(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

// [label, hint] for a notification level. A level this client has no wording
// for yet still shows, under its own name.
export function notifyWording(level) {
  return NOTIFY_WORDING[level] || [titleCase(level), ''];
}

export function recurrenceLabel(recurrence) {
  return titleCase(recurrence);
}

export async function loadMeta() {
  try {
    const m = await api('/meta');
    meta.emoji = m.emoji;
    meta.notifyLevels = m.notify_levels;
    meta.defaultNotifyLevel = m.default_notify_level;
    meta.recurrences = m.recurrences;
  } catch { /* offline: keep the defaults; nothing here is needed to read */ }
}
