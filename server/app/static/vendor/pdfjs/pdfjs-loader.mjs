// PDF.js 4.x ships as an ES module only. app.js is a classic script, so this
// tiny module loads the library and exposes it the way the old UMD build did.
// It is deferred (module semantics) but always resolves long before a user
// can open a PDF; renderPdf checks for window.pdfjsLib at call time.
import * as pdfjsLib from './pdf.min.mjs?v=124';

window.pdfjsLib = pdfjsLib;
