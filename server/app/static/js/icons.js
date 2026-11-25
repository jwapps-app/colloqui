// One source of truth. Each entry is the inner SVG of a 24x24 stroke glyph;
// svgIcon() wraps it. Used for the sidebar/header/footer chrome and the
// per-message action menu so the whole app shares one icon language.
const ICON_PATHS = {
  threads: '<path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/><path d="M8 9h9"/><path d="M8 13h6"/>',
  pin: '<path d="M12 17v5"/><path d="M15 9.34V6a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1v3.34a2 2 0 0 1-.78 1.58l-3.29 2.56a1 1 0 0 0 .61 1.79h11.9a1 1 0 0 0 .61-1.79l-3.27-2.56A2 2 0 0 1 15 9.34z"/>',
  checklist: '<path d="m3 8 2 2 4-4"/><path d="m3 16 2 2 4-4"/><path d="M13 8h8"/><path d="M13 16h8"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  members: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  pencil: '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
  paperclip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  reply: '<polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/>',
  chevron: '<polyline points="9 18 15 12 9 6"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
  minimize: '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/>',
};
export function svgIcon(name, cls) {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON_PATHS[name] || ''}</svg>`;
}

// Overlays are dialogs: announce them as such, name them from their heading,
// and mark them modal so assistive tech treats the background as inert.
export function initDialogSemantics() {
  document.querySelectorAll('.overlay').forEach(ov => {
    ov.setAttribute('role', 'dialog');
    ov.setAttribute('aria-modal', 'true');
    const h = ov.querySelector('h2, h1');
    if (h) {
      if (!h.id) h.id = 'dlg-title-' + (ov.id || Math.random().toString(36).slice(2));
      ov.setAttribute('aria-labelledby', h.id);
    }
  });
  // Placeholder-only inputs get a programmatic label from that placeholder.
  document.querySelectorAll('input[placeholder]:not([aria-label]), textarea[placeholder]:not([aria-label])')
    .forEach(i => { if (!i.labels || !i.labels.length) i.setAttribute('aria-label', i.placeholder); });
  // Icon-only buttons already carry a title; expose it as the accessible name.
  document.querySelectorAll('button[title]:not([aria-label])')
    .forEach(b => { if (!b.textContent.trim()) b.setAttribute('aria-label', b.title); });
}

export function initNavIcons() {
  const byId = {
    'threads-btn': 'threads', 'pins-btn': 'pin', 'tasks-btn': 'checklist',
    'notifs-btn': 'bell', 'settings-btn': 'gear',
    'info-btn': 'info', 'members-btn': 'members',
    'edit-channel-btn': 'pencil', 'attach-btn': 'paperclip',
  };
  for (const [id, name] of Object.entries(byId)) {
    const el = document.getElementById(id);
    if (el) el.insertAdjacentHTML('afterbegin', svgIcon(name, 'nav-ico'));  // before any badge
  }
  const byPane = { 'threads-inbox': 'threads', 'pins': 'pin', 'tasks': 'checklist', 'notifs': 'bell' };
  document.querySelectorAll('.pane-switch [data-pane]').forEach(b => {
    const name = byPane[b.dataset.pane];
    if (name) { b.textContent = ''; b.insertAdjacentHTML('afterbegin', svgIcon(name, 'nav-ico')); }
  });
}
