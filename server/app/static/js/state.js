// The state every part of the client shares. One object, so a module that
// changes it writes `state.x = ...` and the change is visible everywhere.
// State that only one module cares about stays private to that module.
import { relTime } from './format.js';

export const state = {
  token: localStorage.getItem('token'),
  me: null,              // the signed-in user
  spaces: [],
  channels: [],          // every channel and DM the user is in, with its counts
  currentChannel: null,  // the open conversation, or null
  notifUnread: 0,        // unread 🔔 count (also the app-icon badge)
  stickBottom: true,     // is the message list pinned to the newest message?
};

export const presence = new Map();  // user_id -> 'online' | 'away' | 'offline'
export function presenceOf(userId) { return presence.get(userId) || 'offline'; }
export function lastSeenText(user) {
  return user && user.last_seen_at ? 'last seen ' + relTime(user.last_seen_at) : 'offline';
}
