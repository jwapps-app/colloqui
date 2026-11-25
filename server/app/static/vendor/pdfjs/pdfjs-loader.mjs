// PDF.js 4.x ships as an ES module only. This tiny module loads the library
// eagerly and exposes it the way the old UMD build did, so the viewer works
// offline once the app shell has been cached.
// It is deferred (module semantics) but always resolves long before a user
// can open a PDF; renderPdf checks for window.pdfjsLib at call time.
import * as pdfjsLib from './pdf.min.mjs';

window.pdfjsLib = pdfjsLib;
