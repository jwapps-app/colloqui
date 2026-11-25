import { state } from './state.js';

// Pin the layout height (--vh) to what the web view can actually paint. The
// `apple-mobile-web-app-status-bar-style: black` meta seats the iOS standalone
// canvas below the status bar, so the full screen is the paintable height.
//
// Robustness: iOS standalone sometimes reports a STALE, too-small innerHeight
// (or visualViewport.height) after a keyboard/background transition, which used
// to latch a giant gap at the bottom until a force-close. So we take the LARGER
// of the two readings as the full height, and only shrink below it when the
// keyboard is genuinely open — which we gate on an input actually being focused
// (a real keyboard) so a stale small reading alone can't shrink the layout.
function setViewportHeight() {
  const vv = window.visualViewport;
  const inner = window.innerHeight;
  const full = vv ? Math.max(inner, vv.height) : inner;
  let h = full;
  const ae = document.activeElement;
  const typing = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable);
  if (typing && vv) {
    const kb = Math.max(0, full - vv.height - vv.offsetTop);
    if (kb > 120) h = vv.height;  // sit above the on-screen keyboard
  }
  document.documentElement.style.setProperty('--vh', Math.round(h) + 'px');
}
let _lastVH = 0;
// A conversation was just (re)rendered and scrolled to the bottom: take the
// current height as the baseline, so the next real change re-pins the list.
export function syncViewportHeight() {
  _lastVH = parseInt(document.documentElement.style.getPropertyValue('--vh'), 10) || 0;
}
export function keepMessagesPinned() {
  const app = document.getElementById('app');
  if (!app || !app.classList.contains('chat-open')) return;  // no channel open
  const box = document.getElementById('messages');
  if (box && state.stickBottom) box.scrollTop = box.scrollHeight;
}
// Scroll the message list to the newest message. Runs now and again over the
// next couple of frames: WebKit (Safari, incl. the Mac PWA) often hasn't
// finished laying out freshly-inserted messages when we first read
// scrollHeight, which strands the view above the true bottom.
export function scrollMessagesToBottom() {
  const box = document.getElementById('messages');
  if (!box) return;
  const jump = () => { box.scrollTop = box.scrollHeight; };
  jump();
  requestAnimationFrame(() => { jump(); requestAnimationFrame(jump); });
}
// On every viewport change: re-measure, then undo iOS's scroll-into-view. When
// an input is focused, iOS scrolls the layout viewport to reveal it — but the
// body is position:fixed, so that scroll drags the whole UI (and the keyboard)
// up into a stuck state. We already shrink #app above the keyboard, so reset it.
function pinViewport() {
  setViewportHeight();
  if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
  const se = document.scrollingElement;
  if (se && se.scrollTop) se.scrollTop = 0;
  // When the keyboard opens/closes, --vh changes and the list reflows. If the
  // user was reading the latest messages, keep them there instead of stranding
  // them at the oldest message up top.
  const vh = parseInt(document.documentElement.style.getPropertyValue('--vh'), 10) || 0;
  if (vh !== _lastVH) {
    _lastVH = vh;
    keepMessagesPinned();
  }
}
// Re-measure repeatedly for ~2s. iOS standalone reports its true height late at
// startup, and — crucially — reports a STALE, keyboard-sized height for a beat
// after the app returns to the foreground. Re-arming this poll on every
// foreground lets the layout self-heal instead of latching a giant bottom gap
// until a force-close.
let _vhWarmup = null;
function rearmViewportWarmup() {
  let ticks = 0;
  clearInterval(_vhWarmup);
  _vhWarmup = setInterval(() => {
    pinViewport();
    if (++ticks > 14) { clearInterval(_vhWarmup); _vhWarmup = null; }
  }, 150);
}
pinViewport();
rearmViewportWarmup();
['resize', 'orientationchange', 'pageshow', 'focus'].forEach(
  e => window.addEventListener(e, pinViewport)
);
window.addEventListener('pageshow', rearmViewportWarmup);
if (window.visualViewport) {
  // The reliable signal for keyboard open/close on iOS.
  window.visualViewport.addEventListener('resize', pinViewport);
  window.visualViewport.addEventListener('scroll', pinViewport);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Drop keyboard focus on the way out: otherwise a stale "an input is still
    // focused" state on return tricks setViewportHeight into shrinking for a
    // keyboard that isn't actually on screen — the giant-gap latch.
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) ae.blur();
  } else {
    pinViewport();
    rearmViewportWarmup();
  }
});
// iOS does its scroll-into-view shortly after focus and again as the keyboard
// animates in, so snap back across that window too.
document.addEventListener('focusin', () => {
  pinViewport();
  setTimeout(pinViewport, 50);
  setTimeout(pinViewport, 300);
});
