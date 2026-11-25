import { api } from './api.js';
import { loadChannels } from './channels.js';
import { appAlert, appConfirm, appPrompt } from './dialogs.js';
import { $, copyText, emptyRow, titledRow, toast } from './dom.js';
import { relTime } from './format.js';
import { state } from './state.js';

let manageSpaceId = null;

async function loadAdminChannels() {
  const list = $('admin-channel-list');
  const chans = await api('/admin/channels');
  list.innerHTML = '';
  if (chans.length === 0) {
    list.appendChild(emptyRow('No channels.'));
  }
  for (const c of chans) {
    const li = titledRow(
      '# ' + c.name,
      `${c.member_count} member(s)` + (c.is_private ? ' · private' : ''));
    const btn = document.createElement('button');
    btn.textContent = 'Delete';
    btn.onclick = async () => {
      const typed = await appPrompt(
        `This permanently deletes "# ${c.name}" with all its messages and files.\n\nType the channel name to confirm:`,
        { okText: 'Delete', danger: true, placeholder: c.name });
      if (typed === null) return;
      if (typed.trim() !== c.name) { appAlert('Name did not match — nothing was deleted.'); return; }
      try {
        await api(`/channels/${c.id}`, { method: 'DELETE' });
        await Promise.all([loadAdminChannels(), loadChannels()]);
      } catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

export async function openSettings() {
  $('settings').classList.remove('hidden');
  $('apikey-out').innerHTML = '';
  await Promise.all([
    loadAdminSpaces(), loadAdminUsers(), loadAdminInvites(),
    loadAdminChannels(), loadApiKeys(),
  ]);
}

async function loadApiKeys() {
  // Populate the "acts as" dropdown (default to the current admin).
  const sel = $('apikey-user');
  try {
    const users = await api('/users');
    sel.innerHTML = '';
    for (const u of users) {
      const o = document.createElement('option');
      o.value = u.username;
      o.textContent = `${u.display_name} (@${u.username})`;
      if (u.id === state.me.id) o.selected = true;
      sel.appendChild(o);
    }
  } catch {}
  const list = $('admin-apikey-list');
  list.innerHTML = '';
  let keys = [];
  try { keys = await api('/admin/api-keys'); } catch { return; }
  if (keys.length === 0) {
    list.appendChild(emptyRow('No API keys yet.'));
    return;
  }
  for (const k of keys) {
    const li = titledRow(
      k.name,
      `acts as @${k.username || '?'} · created ${relTime(k.created_at)}`
      + (k.last_used_at ? ` · last used ${relTime(k.last_used_at)}` : ' · never used'));
    const btn = document.createElement('button');
    btn.textContent = 'Revoke';
    btn.onclick = async () => {
      if (!await appConfirm(`Revoke the API key “${k.name}”? Anything using it stops working immediately.`,
        { danger: true, okText: 'Revoke' })) return;
      try { await api(`/admin/api-keys/${k.id}`, { method: 'DELETE' }); await loadApiKeys(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

export async function createApiKey() {
  const name = $('apikey-name').value.trim();
  const username = $('apikey-user').value;
  if (!name) { appAlert('Give the key a name.'); return; }
  try {
    const r = await api('/admin/api-keys', {
      method: 'POST', body: JSON.stringify({ name, username }),
    });
    $('apikey-name').value = '';
    const out = $('apikey-out');
    out.innerHTML = '';
    const p = document.createElement('p');
    p.innerHTML = "<b>Copy this key now</b> — it won't be shown again.";
    out.appendChild(p);
    const row = document.createElement('div');
    row.className = 'row';
    const code = document.createElement('input');
    code.readOnly = true; code.value = r.key; code.className = 'grow';
    code.onclick = () => code.select();
    row.appendChild(code);
    const copy = document.createElement('button');
    copy.textContent = 'Copy';
    copy.onclick = async () => {
      if (await copyText(r.key)) toast('Copied');
      else code.select();  // let the user copy by hand
    };
    row.appendChild(copy);
    out.appendChild(row);
    await loadApiKeys();
  } catch (e) { appAlert(e.message); }
}

async function loadAdminUsers() {
  const list = $('admin-user-list');
  const users = await api('/admin/users');
  list.innerHTML = '';
  for (const u of users) {
    const li = titledRow(
      u.display_name,
      '@' + u.username
      + (u.is_admin ? ' · admin' : '') + (u.disabled ? ' · DISABLED' : '')
      + (u.has_password ? ' · password' : '')
      + (u.pending ? ' · not claimed' : ''));
    if (u.id !== state.me.id) {
      const adminBtn = document.createElement('button');
      adminBtn.textContent = u.is_admin ? 'Demote' : 'Make admin';
      adminBtn.onclick = async () => {
        try {
          await api(`/admin/users/${u.id}`, {
            method: 'PATCH', body: JSON.stringify({ is_admin: !u.is_admin }),
          });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(adminBtn);
      const disableBtn = document.createElement('button');
      disableBtn.textContent = u.disabled ? 'Enable' : 'Disable';
      disableBtn.onclick = async () => {
        if (!u.disabled && !await appConfirm(`Disable ${u.display_name}? They are signed out everywhere immediately.`)) return;
        try {
          await api(`/admin/users/${u.id}`, {
            method: 'PATCH', body: JSON.stringify({ disabled: !u.disabled }),
          });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(disableBtn);

      const delBtn = document.createElement('button');
      delBtn.textContent = 'Delete';
      delBtn.onclick = async () => {
        const typed = await appPrompt(
          `Permanently delete @${u.username}. Their messages, files, and passkeys are removed; channels they created are kept under your name. This cannot be undone.\n\nType the username to confirm:`,
          { okText: 'Delete', danger: true, placeholder: u.username });
        if (typed === null) return;
        if (typed.trim() !== u.username) { appAlert('Username did not match — nothing was deleted.'); return; }
        try {
          await api(`/admin/users/${u.id}`, { method: 'DELETE' });
          await loadAdminUsers();
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(delBtn);
    }
    list.appendChild(li);
  }
}

async function loadAdminInvites() {
  const list = $('admin-invite-list');
  const invites = (await api('/admin/invites')).filter(i => !i.used_by);
  list.innerHTML = '';
  if (invites.length === 0) {
    list.appendChild(emptyRow('No pending invites.'));
  }
  for (const inv of invites) {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    const expired = new Date(inv.expires_at) < new Date();
    grow.textContent = (inv.recover_user_id ? 'Recovery invite' : 'Invite')
      + (expired ? ' (expired)' : '');
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `created ${relTime(inv.created_at)} · expires ${relTime(inv.expires_at)}`;
    grow.appendChild(sub);
    li.appendChild(grow);
    const btn = document.createElement('button');
    btn.textContent = 'Revoke';
    btn.onclick = async () => {
      try { await api(`/admin/invites/${inv.id}`, { method: 'DELETE' }); await loadAdminInvites(); }
      catch (e) { appAlert(e.message); }
    };
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function showInvite(inv) {
  const out = $('invite-out');
  out.innerHTML = '';
  out.appendChild(document.createTextNode(
    (inv.recovery_for ? `Recovery code for ${inv.recovery_for}` : 'Code') + ' (shown once): '));
  const code = document.createElement('code');
  code.textContent = inv.code;
  out.appendChild(code);
}

export async function newInvite() {
  try {
    showInvite(await api('/admin/invites', { method: 'POST', body: JSON.stringify({}) }));
  } catch (e) { appAlert(e.message); }
}

export async function newRecoveryInvite() {
  const username = await appPrompt('Username of the account to recover:', { okText: 'Create code' });
  if (!username) return;
  try {
    showInvite(await api('/admin/invites', {
      method: 'POST', body: JSON.stringify({ recover_username: username.trim() }),
    }));
  } catch (e) { appAlert(e.message); }
}

export async function newUser() {
  const username = await appPrompt('Username for the new user:',
    { placeholder: 'e.g. janedoe', okText: 'Next' });
  if (!username) return;
  const display = await appPrompt('Display name:',
    { value: username.trim(), okText: 'Next' });
  if (display === null) return;
  const password = await appPrompt(
    'Optional starter password (min 8 chars) so they can sign in immediately. Leave blank for passkey-only.',
    { okText: 'Create account' });
  if (password === null) return;
  if (password && password.length < 8) { appAlert('Password must be at least 8 characters (or leave blank).'); return; }
  try {
    const body = { username: username.trim(), display_name: display.trim() || username.trim() };
    if (password) body.password = password;
    const res = await api('/admin/users', { method: 'POST', body: JSON.stringify(body) });
    const out = $('new-user-out');
    out.innerHTML = '';
    let msg = `Created @${res.username}. Add them to spaces/channels below. `;
    msg += password
      ? 'They can sign in now with their username + the password you set. Claim code (for adding a passkey later): '
      : 'Give them this claim code (shown once) to enroll their passkey: ';
    out.appendChild(document.createTextNode(msg));
    const code = document.createElement('code');
    code.textContent = res.claim_code;
    out.appendChild(code);
    await loadAdminUsers();
  } catch (e) { appAlert(e.message); }
}

async function openSpaceManage(sp) {
  manageSpaceId = sp.id;
  const canManage = sp.my_role === 'manager' || state.me.is_admin;
  $('space-title').textContent = 'Manage ' + sp.name;
  $('space-name').value = sp.name;
  $('space-name').disabled = !canManage;
  $('space-save').classList.toggle('hidden', !canManage);
  $('space').classList.remove('hidden');
  loadSpaceChannels(sp);
  await loadSpaceMembers(sp);
}

// List the space's channels with up/down reorder controls (managers/admins).
// The order is global and drives everyone's sidebar.
function loadSpaceChannels(sp) {
  const section = $('space-channels-section');
  const canManage = sp.my_role === 'manager' || state.me.is_admin;
  const chans = state.channels.filter(c => !c.is_dm && c.space_id === sp.id);
  section.classList.toggle('hidden', !canManage || chans.length === 0);
  const list = $('space-channel-list');
  list.innerHTML = '';
  const ids = chans.map(c => c.id);
  chans.forEach((c, idx) => {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = '# ' + c.name;
    li.appendChild(grow);
    const up = document.createElement('button');
    up.textContent = '↑'; up.title = 'Move up'; up.disabled = idx === 0;
    up.onclick = () => reorderChannel(sp, ids, idx, -1);
    li.appendChild(up);
    const down = document.createElement('button');
    down.textContent = '↓'; down.title = 'Move down'; down.disabled = idx === chans.length - 1;
    down.onclick = () => reorderChannel(sp, ids, idx, +1);
    li.appendChild(down);
    list.appendChild(li);
  });
}

async function reorderChannel(sp, ids, i, dir) {
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  const arr = ids.slice();
  [arr[i], arr[j]] = [arr[j], arr[i]];
  try {
    await api('/channels/order', {
      method: 'PUT', body: JSON.stringify({ space_id: sp.id, order: arr }),
    });
    await loadChannels();   // re-sort the sidebar
    loadSpaceChannels(sp);  // re-render this list from the new order
  } catch (e) { appAlert(e.message); }
}

async function loadSpaceMembers(sp) {
  const list = $('space-member-list');
  const canManage = sp.my_role === 'manager' || state.me.is_admin;
  $('space-add-input').parentElement.classList.toggle('hidden', !canManage);
  const members = await api(`/spaces/${manageSpaceId}/members`);
  list.innerHTML = '';
  for (const m of members) {
    const li = titledRow(
      m.display_name,
      '@' + m.username + (m.role === 'manager' ? ' · manager' : ''));
    if (canManage && m.id !== state.me.id) {
      const roleBtn = document.createElement('button');
      roleBtn.textContent = m.role === 'manager' ? 'Make member' : 'Make manager';
      roleBtn.onclick = async () => {
        try {
          await api(`/spaces/${manageSpaceId}/members/${m.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ role: m.role === 'manager' ? 'member' : 'manager' }),
          });
          await loadSpaceMembers(sp);
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(roleBtn);
      if (!sp.is_default) {
        const rm = document.createElement('button');
        rm.textContent = 'Remove';
        rm.onclick = async () => {
          if (!await appConfirm(`Remove ${m.display_name} from ${sp.name}?`)) return;
          try {
            await api(`/spaces/${manageSpaceId}/members/${m.id}`, { method: 'DELETE' });
            await loadSpaceMembers(sp);
          } catch (e) { appAlert(e.message); }
        };
        li.appendChild(rm);
      }
    }
    list.appendChild(li);
  }
}

export async function addSpaceMember() {
  const username = $('space-add-input').value.trim().toLowerCase();
  if (!username || !manageSpaceId) return;
  try {
    const user = (await api('/users')).find(u => u.username === username);
    if (!user) throw new Error('No such user');
    await api(`/spaces/${manageSpaceId}/members`, {
      method: 'POST', body: JSON.stringify({ user_id: user.id }),
    });
    $('space-add-input').value = '';
    const sp = state.spaces.find(s => s.id === manageSpaceId) || { id: manageSpaceId };
    await loadSpaceMembers(sp);
  } catch (e) { appAlert(e.message); }
}

export async function saveSpaceName() {
  const name = $('space-name').value.trim();
  if (!name || !manageSpaceId) return;
  try {
    await api(`/spaces/${manageSpaceId}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    $('space').classList.add('hidden');
    await loadChannels();
    // If we came from Settings → Spaces, refresh that list too.
    if (!$('settings').classList.contains('hidden')) await loadAdminSpaces();
  } catch (e) { appAlert(e.message); }
}

async function reorderSpace(ids, i, dir) {
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  const arr = ids.slice();
  [arr[i], arr[j]] = [arr[j], arr[i]];
  try {
    await api('/spaces/order', { method: 'PUT', body: JSON.stringify({ order: arr }) });
    await Promise.all([loadAdminSpaces(), loadChannels()]);
  } catch (e) { appAlert(e.message); }
}

async function loadAdminSpaces() {
  const list = $('admin-space-list');
  const all = await api('/spaces');
  const ids = all.map(s => s.id);
  list.innerHTML = '';
  all.forEach((sp, idx) => {
    const li = document.createElement('li');
    const grow = document.createElement('span');
    grow.className = 'grow';
    grow.textContent = sp.name + (sp.is_default ? ' (default)' : '');
    li.appendChild(grow);
    const up = document.createElement('button');
    up.textContent = '↑'; up.title = 'Move up'; up.disabled = idx === 0;
    up.onclick = () => reorderSpace(ids, idx, -1);
    li.appendChild(up);
    const down = document.createElement('button');
    down.textContent = '↓'; down.title = 'Move down'; down.disabled = idx === all.length - 1;
    down.onclick = () => reorderSpace(ids, idx, +1);
    li.appendChild(down);
    const manage = document.createElement('button');
    manage.textContent = 'Manage';
    manage.title = 'Rename this space or add members';
    manage.onclick = () => openSpaceManage(sp);
    li.appendChild(manage);
    if (!sp.is_default) {
      const del = document.createElement('button');
      del.textContent = 'Delete';
      del.onclick = async () => {
        const typed = await appPrompt(
          `Permanently delete the space "${sp.name}" and all its channels, messages, and files.\n\nType the space name to confirm:`,
          { okText: 'Delete', danger: true, placeholder: sp.name });
        if (typed === null) return;
        if (typed.trim() !== sp.name) { appAlert('Name did not match — nothing was deleted.'); return; }
        try {
          await api(`/spaces/${sp.id}`, { method: 'DELETE' });
          await Promise.all([loadAdminSpaces(), loadChannels()]);
        } catch (e) { appAlert(e.message); }
      };
      li.appendChild(del);
    }
    list.appendChild(li);
  });
}

export async function newSpace() {
  const name = $('new-space-name').value.trim();
  if (!name) return;
  try {
    await api('/spaces', { method: 'POST', body: JSON.stringify({ name }) });
    $('new-space-name').value = '';
    await Promise.all([loadAdminSpaces(), loadChannels()]);
  } catch (e) { appAlert(e.message); }
}
