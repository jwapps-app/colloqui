// Pure formatting helpers: no DOM, no app state.

export const b64u = {
  enc: buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  dec: s => Uint8Array.from(
    atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')),
    c => c.charCodeAt(0)
  ).buffer,
};

export function relTime(iso) {
  const d = new Date(iso), now = new Date(), diff = (now - d) / 1000;
  if (diff < 60) return 'now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

export function shortDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

// "1 reply", "3 replies".
export function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}
