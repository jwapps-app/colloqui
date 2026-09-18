// Set the theme before first paint to avoid a flash of the wrong palette.
// Lives in its own file (not inline) because the app's CSP allows scripts from
// 'self' only; an inline script was silently blocked and never ran.
(function () {
  var p = localStorage.getItem('theme') || 'system';
  var dark = p === 'dark' ||
    (p !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
