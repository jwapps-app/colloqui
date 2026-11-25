import { signOutLocal } from './auth.js';
import { state } from './state.js';

export async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch('/api/v1' + path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    // Only an expired/revoked *session* means we're signed out — a 401 from
    // a login attempt should surface its real error message.
    if (res.status === 401 && state.token) signOutLocal();
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}
