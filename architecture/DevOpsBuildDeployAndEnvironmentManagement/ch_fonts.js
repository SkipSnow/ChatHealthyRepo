// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Single source of truth for ChatHealthy.ai font sizing.
//
// ONE number in ONE place: CH_BASE below. Everything in the codebase
// is expressed in em relative to this. Change the number, every font
// and dimension in the app rescales.
//
// chFont(scale) returns the corresponding em string for inline-style
// use: chFont(1) -> "1em" (= the base), chFont(0.5) halves, chFont(2)
// doubles.

(function () {
  var CH_BASE = '6px';   // ← THE ONLY hard-coded length in the codebase.

  var CH_FONT_FAMILY =
    'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  var CH_MONO_FAMILY =
    'ui-monospace, Menlo, Consolas, "Liberation Mono", monospace';

  function chFont(scale) {
    var s = (typeof scale === 'number' && isFinite(scale) && scale > 0) ? scale : 1;
    return s + 'em';
  }
  window.chFont = chFont;
  window.CH_FONT_FAMILY = CH_FONT_FAMILY;
  window.CH_MONO_FAMILY = CH_MONO_FAMILY;

  // Inject the base font-size on <html> and the family + 1rem-on-everything
  // override. `!important` wins over every inline font-size in the codebase
  // so the registry governs uniformly. With html at CH_BASE, every em
  // across the app resolves relative to that one number.
  var css =
    'html { font-size: ' + CH_BASE + ' !important; }' +
    'body, input, button, select, textarea, div, span, p, a, ' +
    'h1, h2, h3, h4, h5, h6, label, li, td, th, dd, dt, code {' +
      'font-family:' + CH_FONT_FAMILY + ' !important;' +
      'font-size: 1rem !important;' +
    '}' +
    'code, pre, .ch-mono {' +
      'font-family:' + CH_MONO_FAMILY + ' !important;' +
    '}';

  function inject() {
    var s = document.createElement('style');
    s.id = 'ch-fonts';
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }
  if (document.head) {
    inject();
  } else {
    document.addEventListener('DOMContentLoaded', inject, { once: true });
  }
})();
