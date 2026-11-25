import { $ } from './dom.js';

let dialogResolve = null;

function openDialog(opts) {
  return new Promise(resolve => {
    dialogResolve = resolve;
    const title = $('dialog-title');
    title.textContent = opts.title || '';
    title.classList.toggle('hidden', !opts.title);
    const msg = $('dialog-message');
    msg.textContent = opts.message || '';
    msg.classList.toggle('hidden', !opts.message);
    const input = $('dialog-input');
    const ta = $('dialog-textarea');
    const sel = $('dialog-select');
    input.classList.add('hidden');
    ta.classList.add('hidden');
    sel.classList.add('hidden');
    let field = null;
    if (opts.input === 'select') {
      field = sel;
      sel.innerHTML = '';
      for (const o of (opts.options || [])) {
        const opt = document.createElement('option');
        opt.value = o.value;
        opt.textContent = o.label;
        if (o.value === (opts.value || '')) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.classList.remove('hidden');
    } else {
      field = opts.input === 'textarea' ? ta : opts.input === 'text' ? input : null;
      if (field) {
        field.classList.remove('hidden');
        field.value = opts.value || '';
        field.placeholder = opts.placeholder || '';
      }
    }
    const ok = $('dialog-ok');
    const cancel = $('dialog-cancel');
    ok.textContent = opts.okText || 'OK';
    ok.classList.toggle('danger-btn', !!opts.danger);
    cancel.classList.toggle('hidden', opts.noCancel === true);
    $('dialog').dataset.mode = opts.input ? 'input' : 'confirm';
    $('dialog').classList.remove('hidden');
    if (field) setTimeout(() => { field.focus(); if (field.select) field.select(); }, 0);
    else setTimeout(() => ok.focus(), 0);
  });
}

export function resolveDialog(confirmed) {
  const d = $('dialog');
  if (d.classList.contains('hidden')) return;
  d.classList.add('hidden');
  const resolve = dialogResolve;
  dialogResolve = null;
  if (!resolve) return;
  if (d.dataset.mode === 'input') {
    if (!confirmed) return resolve(null);
    const sel = $('dialog-select');
    if (!sel.classList.contains('hidden')) return resolve(sel.value);
    const ta = $('dialog-textarea');
    resolve(ta.classList.contains('hidden') ? $('dialog-input').value : ta.value);
  } else {
    resolve(confirmed);
  }
}

export function appAlert(message, title) {
  return openDialog({ title, message, okText: 'OK', noCancel: true });
}
export function appConfirm(message, opts = {}) {
  return openDialog({ title: opts.title, message, okText: opts.okText || 'Confirm', danger: opts.danger });
}
export function appPrompt(message, opts = {}) {
  return openDialog({
    title: opts.title, message, input: opts.multiline ? 'textarea' : 'text',
    value: opts.value, placeholder: opts.placeholder, okText: opts.okText || 'Save',
  });
}
// options: [{ value, label }]. Resolves to the chosen value, or null if cancelled.
export function appChoose(message, options, opts = {}) {
  return openDialog({
    title: opts.title, message, input: 'select', options,
    value: opts.value, okText: opts.okText || 'Save',
  });
}

// Run an action started by the user; if it fails, show them why instead of
// throwing. Resolves to whether it succeeded. Use this for a click handler
// whose only error handling would be `catch (e) { appAlert(e.message) }`;
// reserve a silent `catch {}` for work that is genuinely best-effort.
export async function guarded(action) {
  try {
    await action();
    return true;
  } catch (e) {
    appAlert(e.message);
    return false;
  }
}
