import { appAlert } from './dialogs.js';
import { $ } from './dom.js';
import { svgIcon } from './icons.js';
import { closeInfoPane } from './infopane.js';
import { state } from './state.js';
import { closeThread } from './threads.js';

export const imageUrls = new Map();

const IMAGE_CACHE_MAX = 60;  // bounded LRU so a long session can't leak blob URLs
async function loadAuthedFile(fileId) {
  const cached = imageUrls.get(fileId);
  if (cached) {
    imageUrls.delete(fileId); imageUrls.set(fileId, cached);  // mark most-recent
    return cached;
  }
  const res = await fetch(`/api/v1/files/${fileId}`, {
    headers: { Authorization: 'Bearer ' + state.token },
  });
  if (!res.ok) throw new Error('File unavailable');
  const blob = await res.blob();
  const entry = { blob, url: URL.createObjectURL(blob) };
  imageUrls.set(fileId, entry);
  // Evict the oldest and free its object URL once over the cap.
  while (imageUrls.size > IMAGE_CACHE_MAX) {
    const oldest = imageUrls.keys().next().value;
    const ev = imageUrls.get(oldest);
    imageUrls.delete(oldest);
    if (ev) URL.revokeObjectURL(ev.url);
  }
  return entry;
}

export async function loadAuthedBlob(fileId) {
  return (await loadAuthedFile(fileId)).url;
}

// Preview only types that are inert when rendered. Never frame anything
// HTML-ish: a blob iframe runs in our origin, so that would be self-XSS.
export function previewKind(contentType) {
  if (contentType.startsWith('image/')) return 'image';
  if (contentType.startsWith('video/')) return 'video';
  if (contentType.startsWith('audio/')) return 'audio';
  if (contentType === 'application/pdf') return 'pdf';
  if (contentType === 'text/plain' || contentType === 'text/csv'
      || contentType === 'text/markdown' || contentType === 'application/json') return 'text';
  return null;
}

export async function downloadFile(file) {
  try {
    const url = await loadAuthedBlob(file.id);
    const dl = document.createElement('a');
    dl.href = url; dl.download = file.filename;
    dl.click();
  } catch (err) { appAlert(err.message); }
}

export function closePreview() {
  const pane = $('preview-pane');
  pane.classList.add('hidden');
  pane.classList.remove('expanded');
  updateExpandIcon(false);
  $('viewer-content').innerHTML = '';
  if (_pdfDoc) { _pdfDoc.destroy(); _pdfDoc = null; }
}

// Bumped on every openViewer call; an in-flight PDF render bails if a newer
// file was opened meanwhile (rendering is async and page-by-page).
let _viewerGen = 0;
let _pdfWorkerSet = false;
let _pdfDoc = null;   // current PDF.js doc, kept so expand/collapse can re-render crisply
const PDF_MAX_PAGES = 50;

// Draw a PDF's pages as stacked <canvas>es sized to the current pane width.
// Split out from renderPdf so expand/collapse can re-rasterize crisply instead
// of upscaling the existing canvases.
async function renderPdfPages(pdf, box, gen) {
  box.innerHTML = '';
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const targetW = (box.clientWidth || 600) - 28;  // minus #viewer-content padding
  const pages = Math.min(pdf.numPages, PDF_MAX_PAGES);
  for (let n = 1; n <= pages; n++) {
    const page = await pdf.getPage(n);
    if (gen !== _viewerGen || _pdfDoc !== pdf) return;  // superseded
    const scale = Math.max(0.2, targetW / page.getViewport({ scale: 1 }).width);
    const vp = page.getViewport({ scale: scale * dpr });
    const canvas = document.createElement('canvas');
    canvas.className = 'pdf-page';
    canvas.width = vp.width; canvas.height = vp.height;
    box.appendChild(canvas);
    await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
  }
  if (pdf.numPages > pages) {
    const note = document.createElement('p');
    note.className = 'muted pdf-note';
    note.textContent = `Showing the first ${pages} of ${pdf.numPages} pages — use Download for the whole file.`;
    box.appendChild(note);
  }
}

// Render a PDF blob via the self-hosted PDF.js. Works in every browser (incl.
// WebKit/iOS, which can't frame a blob PDF) and keeps the doc in the pane.
async function renderPdf(entry, box, gen) {
  // PDF.js is loaded by its own module script (vendor/pdfjs/pdfjs-loader.mjs).
  const pdfjsLib = window.pdfjsLib;
  if (!pdfjsLib) throw new Error('PDF viewer failed to load');
  if (!_pdfWorkerSet) {
    // Resolved against this module, so the worker comes from the same build.
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
    _pdfWorkerSet = true;
  }
  const data = await entry.blob.arrayBuffer();
  if (gen !== _viewerGen) return;
  // isEvalSupported: false — never compile font programs with new Function,
  // the code path behind CVE-2024-4367. The CSP already forbids eval; this
  // makes the viewer safe even if that header were ever relaxed.
  const pdf = await pdfjsLib.getDocument({ data, isEvalSupported: false }).promise;
  if (gen !== _viewerGen) { pdf.destroy(); return; }
  _pdfDoc = pdf;
  await renderPdfPages(pdf, box, gen);
}

function updateExpandIcon(expanded) {
  const b = $('viewer-expand');
  if (!b) return;
  b.innerHTML = svgIcon(expanded ? 'minimize' : 'maximize', 'nav-ico');
  b.title = expanded ? 'Exit full window' : 'Expand to full window';
}

// Toggle the preview pane between the side column and a full-window overlay.
// The ✕ still returns to Colloqui; this just changes how much room the file gets.
export function toggleViewerExpand() {
  const expanded = $('preview-pane').classList.toggle('expanded');
  updateExpandIcon(expanded);
  // Re-rasterize the PDF at the new width so it stays sharp, not upscaled. Wait
  // a frame so the width change lands before we measure the box.
  if (_pdfDoc) {
    const box = $('viewer-content');
    requestAnimationFrame(() => renderPdfPages(_pdfDoc, box, _viewerGen).catch(() => {}));
  }
}

export async function openViewer(file) {
  const kind = previewKind(file.content_type);
  if (!kind) return downloadFile(file);
  closeInfoPane();
  $('viewer-title').textContent = file.filename;
  $('viewer-download').onclick = () => downloadFile(file);
  const box = $('viewer-content');
  box.innerHTML = '';
  if (_pdfDoc) { _pdfDoc.destroy(); _pdfDoc = null; }
  const gen = ++_viewerGen;
  closeThread();
  $('preview-pane').classList.remove('hidden');
  updateExpandIcon($('preview-pane').classList.contains('expanded'));
  try {
    const entry = await loadAuthedFile(file.id);
    if (gen !== _viewerGen) return;
    if (kind === 'text') {
      if (entry.blob.size > 1024 * 1024) throw new Error('Too large to preview — use Download');
      const pre = document.createElement('pre');
      pre.textContent = await entry.blob.text();
      box.appendChild(pre);
      return;
    }
    if (kind === 'pdf') {
      await renderPdf(entry, box, gen);
      return;
    }
    const el = kind === 'image' ? document.createElement('img')
      : kind === 'video' ? document.createElement('video')
      : document.createElement('audio');
    if (kind === 'video' || kind === 'audio') el.controls = true;
    if (kind === 'image') el.alt = file.filename;
    el.src = entry.url;
    box.appendChild(el);
  } catch (err) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = err.message;
    box.appendChild(p);
  }
}
