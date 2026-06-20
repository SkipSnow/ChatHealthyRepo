// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ScrollableFrame — single canonical facade for any component that needs
// to scroll inside its allocated frame. One widget, called everywhere.
//
//   ScrollableFrame.wrap(innerHtml, opts?) -> HTML string
//     Wraps innerHtml in a div that fills its parent (flex:1, min-height:0)
//     and scrolls vertically when content overflows. Caller decides where
//     the wrapper mounts; the widget owns the scroll behavior. opts:
//       { padding, background, dataTestId }
//
//   ScrollableFrame.apply(el, opts?) -> el
//     Applies the same canonical scroll behavior to an existing DOM
//     element. Use when you already have the container in hand.
//
// Constraints on the caller: the parent of the scrollable frame must
// itself have a definite height (height: 100% inside a flex column, or
// height: 100vh on body) and min-height: 0 if it's a flex item. Without
// that the flex:1 here can't compute a height.
(function () {
  var BASE_STYLE = 'flex:1;min-height:0;overflow-y:auto;';

  function esc(s) {
    return String(s == null ? '' : s)
      .split('&').join('&amp;')
      .split('<').join('&lt;')
      .split('>').join('&gt;')
      .split('"').join('&quot;')
      .split("'").join('&#39;');
  }

  function buildStyle(opts) {
    var style = BASE_STYLE;
    if (opts && opts.padding) style += 'padding:' + opts.padding + ';';
    if (opts && opts.background) style += 'background:' + opts.background + ';';
    return style;
  }

  function wrap(innerHtml, opts) {
    var attr = '';
    if (opts && opts.id) attr += ' id="' + esc(opts.id) + '"';
    if (opts && opts.dataTestId) attr += ' data-testid="' + esc(opts.dataTestId) + '"';
    return '<div class="ch-scroll-frame"' + attr + ' style="' + buildStyle(opts) + '">'
      + (innerHtml == null ? '' : innerHtml)
      + '</div>';
  }

  function apply(el, opts) {
    if (!el) return el;
    el.classList.add('ch-scroll-frame');
    el.style.flex = '1';
    el.style.minHeight = '0';
    el.style.overflowY = 'auto';
    if (opts && opts.padding) el.style.padding = opts.padding;
    if (opts && opts.background) el.style.background = opts.background;
    if (opts && opts.dataTestId) el.setAttribute('data-testid', opts.dataTestId);
    return el;
  }

  window.ScrollableFrame = { wrap: wrap, apply: apply };
})();
