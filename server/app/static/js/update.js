// Keeping the installed app current, and the service worker that makes it an
// installable app in the first place.
//
// Every asset is served under /assets/<build>/, where <build> names the exact
// set of files the server is running (see app/assets.py). The server stamps
// that id into index.html; nothing in the client carries a version number.
import { openChannelFromLink } from './channels.js';

// The build this page was loaded as.
const BUILD = document.querySelector('meta[name="build"]')?.content || '';

if ('serviceWorker' in navigator) {
  // Registering the worker is what lets a launch load the latest shell from
  // the network (it defeats iOS standalone's stale page cache).
  navigator.serviceWorker.register('/sw.js').catch(() => {});
  // A tapped notification (app already open) asks us to jump to its channel.
  navigator.serviceWorker.addEventListener('message', e => {
    if (e.data && e.data.type === 'open-channel') {
      openChannelFromLink(e.data.channelId, e.data.rootId);
    }
  });
  // Assets of earlier builds are dead weight in the offline cache: drop them.
  navigator.serviceWorker.ready
    .then(reg => { if (reg.active && BUILD) reg.active.postMessage({ type: 'prune-assets', build: BUILD }); })
    .catch(() => {});
}

// Auto-update: iOS standalone caches the start page at the OS level, so a cold
// launch can run stale code. On load (and when the app is foregrounded) ask
// the server which build it is serving, and reload if that is not the one
// running. A per-session cap prevents reload loops.
async function checkForUpdate() {
  try {
    const d = await (await fetch('/healthz', { cache: 'no-store' })).json();
    // Show the real release version (the git tag baked into the image).
    const el = document.getElementById('ver');
    if (el && d.version) el.textContent = d.version;
    if (!d.build || !BUILD) return;
    if (d.build === BUILD) { sessionStorage.removeItem('updTries'); return; }
    const tries = +(sessionStorage.getItem('updTries') || 0);
    if (tries < 2) { sessionStorage.setItem('updTries', tries + 1); location.reload(); }
  } catch {}
}
checkForUpdate();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkForUpdate();
});
