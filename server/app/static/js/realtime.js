import {
  channelById,
  clearChannelView,
  loadChannels,
  markRead,
  renderChannels,
  selectChannel,
} from './channels.js';
import { memberCache } from './composer.js';
import { $ } from './dom.js';
import { scheduleInfoPane } from './infopane.js';
import { buildMessageNode, linkCardsEl, renderMessage, renderReactions } from './messages.js';
import { loadNotifications, maybeBrowserNotify, updateNotifBadge } from './notifications.js';
import { scheduleTaskCount, scheduleThreadCount } from './panes.js';
import { loadReminders } from './reminders.js';
import { replyCount } from './rows.js';
import { presence, presenceOf, state } from './state.js';
import { hasTaskLine } from './tasks.js';
import { appendThreadReply, threadRootId, updateThreadSummary } from './threads.js';

  // id of the thread currently open in the thread pane
let sock = null;
export const typers = new Map();   // user_id -> {name, timer}
let lastTypingSent = 0;

export function renderTyping() {
  const names = [...typers.values()].map(t => t.name);
  $('typing').textContent = names.length === 0 ? ''
    : names.length === 1 ? `${names[0]} is typing…`
    : `${names.join(', ')} are typing…`;
}

function onTypingEvent(data) {
  if (!state.currentChannel || data.channel_id !== state.currentChannel.id) return;
  const existing = typers.get(data.user.id);
  if (existing) clearTimeout(existing.timer);
  typers.set(data.user.id, {
    name: data.user.display_name,
    timer: setTimeout(() => { typers.delete(data.user.id); renderTyping(); }, 4000),
  });
  renderTyping();
}

export function maybeSendTyping() {
  if (!state.currentChannel || !sock || sock.readyState !== WebSocket.OPEN) return;
  const now = Date.now();
  if (now - lastTypingSent < 2500) return;
  lastTypingSent = now;
  sock.send(JSON.stringify({ type: 'typing', channel_id: state.currentChannel.id }));
}

function handleEvent(data) {
  if (data.type === 'ready') {
    presence.clear();
    for (const [id, state] of Object.entries(data.presence || {})) presence.set(id, state);
    onPresenceChanged();
    // Sync this client's state to the fresh socket (server assumes active).
    myPresence = 'active';
    if (document.hidden) sendPresence('away'); else armIdle();
    if (wsEverConnected) resyncAfterReconnect();
    wsEverConnected = true;
  } else if (data.type === 'presence') {
    if (data.state && data.state !== 'offline') presence.set(data.user_id, data.state);
    else presence.delete(data.user_id);
    onPresenceChanged();
  } else if (data.type === 'typing') {
    onTypingEvent(data);
  } else if (data.type === 'notification') {
    state.notifUnread++;
    updateNotifBadge();
    maybeBrowserNotify(data.notification);
    if (!$('notifs').classList.contains('hidden')) {
      loadReminders(); loadNotifications();
    }
  } else if (data.type === 'alert') {
    // Live "All"-level ping: a desktop/browser popup when you're away, but no
    // bell badge and no inbox entry — keeps the 🔔 list meaningful.
    maybeBrowserNotify(data);
  } else if (data.type === 'channels.changed') {
    memberCache.clear();  // membership may have changed
    loadChannels();
  } else if (data.type === 'channel.updated') {
    loadChannels();
  } else if (data.type === 'channel.deleted') {
    if (state.currentChannel && state.currentChannel.id === data.channel_id) clearChannelView();
    loadChannels();
  } else if (data.type === 'message.created') {
    const m = data.message;
    const ch = channelById(m.channel_id);
    if (m.thread_root_id) {
      // A thread reply: it belongs in the thread view, never the timeline.
      if (threadRootId === m.thread_root_id
          && !$('thread-pane').classList.contains('hidden')) {
        appendThreadReply(m);
        if (state.currentChannel && m.channel_id === state.currentChannel.id) markRead(state.currentChannel.id);
      }
      if (ch) {
        // Thread replies aren't in the channel timeline, so they don't count
        // toward the channel's message count — only unread activity.
        const current = state.currentChannel && m.channel_id === state.currentChannel.id;
        if (!current && (!state.me || m.sender.id !== state.me.id)) {
          ch.unread_count = (ch.unread_count || 0) + 1;
        }
        renderChannels();
      } else {
        loadChannels();
      }
      // A thread reply always changes the threads inbox; refresh task counts
      // only if it actually carries a checkbox.
      if (hasTaskLine(m.content)) scheduleTaskCount();
      scheduleThreadCount();
      return;
    }
    if (state.currentChannel && m.channel_id === state.currentChannel.id) {
      typers.delete(m.sender.id); renderTyping();
      renderMessage(m);
      // Follow the conversation only if the user is already at (or near) the
      // bottom, or it's their own message — don't yank them out of history.
      if (state.stickBottom || (state.me && m.sender.id === state.me.id)) {
        const box = $('messages');
        box.scrollTop = box.scrollHeight;
      }
      if (ch) {
        ch.message_count = (ch.message_count || 0) + 1;
        ch.recent_count = (ch.recent_count || 0) + 1;
      }
      markRead(state.currentChannel.id);  // keep the read marker current
      renderChannels();
    } else if (ch) {
      ch.message_count = (ch.message_count || 0) + 1;
      ch.recent_count = (ch.recent_count || 0) + 1;
      if (!state.me || m.sender.id !== state.me.id) ch.unread_count = (ch.unread_count || 0) + 1;
      renderChannels();
    } else {
      loadChannels();  // a channel we don't have yet
    }
    // A plain top-level message only touches task counts if it has a checkbox;
    // it doesn't affect the threads inbox until it gets a reply.
    if (hasTaskLine(m.content)) scheduleTaskCount();
  } else if (data.type === 'message.updated') {
    const id = data.message.id;
    // Remember whether the OLD rendering had a task line: an edit that removes
    // the last checkbox must refresh counts too, not just one that adds one.
    let hadTask = false;
    if (state.currentChannel && data.message.channel_id === state.currentChannel.id) {
      const el = document.querySelector(`#messages .msg[data-id="${id}"]`);
      if (el) {
        hadTask = !!el.querySelector('.task-line, input[type="checkbox"]');
        el.replaceWith(buildMessageNode(data.message));
      }
    }
    const tp = document.querySelector(`#thread-content .msg[data-id="${id}"]`);
    if (tp) {
      const fresh = buildMessageNode(data.message, { inThread: true });
      if (tp.classList.contains('thread-root-msg')) fresh.classList.add('thread-root-msg');
      tp.replaceWith(fresh);
    }
    if (hadTask || hasTaskLine(data.message.content)) scheduleTaskCount();
  } else if (data.type === 'message.deleted') {
    document.querySelectorAll(`.msg[data-id="${data.message.id}"]`).forEach(el => el.remove());
    const c = $('thread-count');
    if (c) {
      const n = document.querySelectorAll('#thread-content .msg').length - 1;
      if (n >= 0) c.textContent = replyCount(n);
    }
    const ch = channelById(data.message.channel_id);
    // Only top-level (non-thread-reply) messages count toward the channel total.
    if (ch && !data.message.thread_root_id) {
      if (ch.message_count) ch.message_count--;
      if (ch.recent_count) ch.recent_count--;
      renderChannels();
    }
    scheduleTaskCount();
    scheduleThreadCount();
  } else if (data.type === 'message.reacted') {
    document.querySelectorAll(`.msg[data-id="${data.message_id}"] .reactions`)
      .forEach(row => renderReactions(row, data.message_id, data.reactions));
  } else if (data.type === 'message.preview') {
    document.querySelectorAll(`.msg[data-id="${data.message_id}"]`).forEach(node => {
      const main = node.querySelector('.msg-main');
      if (!main || main.querySelector('.link-cards')) return;
      const cards = linkCardsEl(data.previews);
      const rrow = main.querySelector('.reactions');
      if (rrow) main.insertBefore(cards, rrow); else main.appendChild(cards);
    });
  } else if (data.type === 'thread.updated') {
    updateThreadSummary(data.root_id, data);
    if (state.currentChannel && data.channel_id === state.currentChannel.id) scheduleInfoPane();
  } else if (data.type === 'pins.changed') {
    if (state.currentChannel && data.channel_id === state.currentChannel.id) scheduleInfoPane();
    // The sidebar's pinned count and the pins-icon state come from the channel
    // list; refresh it so a pin/unpin shows up there too, not just in the pane.
    loadChannels().catch(() => {});
  }
}

// Re-render presence dots wherever they appear. DM dots are rebuilt by
// renderChannels; member-list and footer dots carry data-uid and update here.
function onPresenceChanged() {
  // Patch just the presence dots (DM list, member pane) instead of rebuilding
  // the whole sidebar on every online/away/offline flip.
  document.querySelectorAll('.dot[data-uid]').forEach(d => {
    d.className = 'dot ' + presenceOf(d.dataset.uid);
  });
}
let myPresence = 'active';
let idleTimer = null;
const IDLE_MS = 5 * 60 * 1000;
function sendPresence(state) {
  if (state === myPresence) return;
  myPresence = state;
  if (sock && sock.readyState === WebSocket.OPEN) {
    sock.send(JSON.stringify({ type: 'presence', state }));
  }
}
function armIdle() {
  sendPresence('active');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => sendPresence('away'), IDLE_MS);
}
['mousemove', 'keydown', 'touchstart', 'pointerdown'].forEach(
  e => window.addEventListener(e, armIdle, { passive: true })
);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) sendPresence('away'); else armIdle();
});

let wsPingTimer = null;
let wsEverConnected = false;   // a later 'ready' is a RE-connect: resync state
let wsRetryMs = 3000;          // reconnect backoff, reset on a good connection
let wsPongDue = 0;             // ms timestamp by which a pong must arrive
// Close the socket for good (sign-out): no reconnect, no keepalive.
export function disconnectWs() {
  if (sock) { sock.onclose = null; sock.close(); sock = null; }
  clearInterval(wsPingTimer);
}

export function connectWs() {
  if (sock) { sock.onclose = null; sock.close(); }
  clearInterval(wsPingTimer);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const s = new WebSocket(`${proto}//${location.host}/api/v1/ws`);
  sock = s;
  // Handlers close over THIS socket, so a stale socket's close can't restart
  // or clear the timers of a newer connection.
  s.onopen = () => {
    if (sock !== s) return;
    s.send(JSON.stringify({ token: state.token }));
    wsRetryMs = 3000;
    // Keep the socket alive through Cloudflare Tunnel's ~100s idle timeout, and
    // detect a silently dropped (half-open) connection: a ping must be answered
    // by the next tick or we treat the socket as dead and reconnect.
    clearInterval(wsPingTimer);
    wsPongDue = 0;
    wsPingTimer = setInterval(() => {
      if (sock !== s || s.readyState !== WebSocket.OPEN) return;
      if (wsPongDue && Date.now() > wsPongDue) { s.close(); return; }  // no pong: dead
      wsPongDue = Date.now() + 25000;
      s.send(JSON.stringify({ type: 'ping' }));
    }, 30000);
  };
  s.onmessage = ev => {
    if (sock !== s) return;
    const data = JSON.parse(ev.data);
    if (data.type === 'pong') { wsPongDue = 0; return; }
    handleEvent(data);
  };
  s.onclose = () => {
    if (sock !== s) return;
    clearInterval(wsPingTimer);
    if (!state.token) return;
    setTimeout(connectWs, wsRetryMs);
    wsRetryMs = Math.min(wsRetryMs * 2, 60000);
  };
}

// After a reconnect, anything that happened while the socket was down was
// missed (the socket is the only live feed). Refresh the channel list (counts)
// and reload the open conversation so the view catches up.
async function resyncAfterReconnect() {
  try {
    await loadChannels();
    if (state.currentChannel) {
      const ch = channelById(state.currentChannel.id);
      if (ch) await selectChannel(ch);
    }
  } catch {}
}
