import { state } from './state.js';

export const avatarCache = new Map();

function colorFor(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 42%)`;
}

async function loadAvatar(user) {
  const key = `${user.id}:${user.avatar_at}`;
  if (avatarCache.has(key)) return avatarCache.get(key);
  const res = await fetch(`/api/v1/users/${user.id}/avatar`, {
    headers: { Authorization: 'Bearer ' + state.token },
  });
  if (!res.ok) throw new Error('no avatar');
  const url = URL.createObjectURL(await res.blob());
  avatarCache.set(key, url);
  return url;
}

export function avatarEl(user) {
  const el = document.createElement('span');
  el.className = 'avatar';
  if (user.avatar_at) {
    const img = document.createElement('img');
    img.alt = user.display_name;
    loadAvatar(user).then(url => { img.src = url; }).catch(() => {
      el.textContent = (user.display_name || '?').trim().charAt(0).toUpperCase();
      el.style.background = colorFor(user.id);
    });
    el.appendChild(img);
  } else {
    el.textContent = (user.display_name || '?').trim().charAt(0).toUpperCase();
    el.style.background = colorFor(user.id);
  }
  return el;
}
