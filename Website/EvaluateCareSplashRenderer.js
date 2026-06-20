// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// EvaluateCareSplashRenderer(payload, target, task)
// Partner tool: evaluateCare/Code/displayChrome/splash_endpoint.py
//   class SplashEndpoint
(function () {
  function EvaluateCareSplashRenderer(payload, target, task) {
    if (typeof target === 'string') target = document.getElementById(target);
    var html = (payload && typeof payload.html === 'string') ? payload.html : '';
    // Frame discipline (EPIC-002-F-011): the splash occupies the whole
    // centerContent as cells, mirroring FindCare's renderProviderResults
    // shape — header cell + body cell — so EvaluateCare is using "the
    // one and only frame" rather than floating minimal text.
    var wrapped = ''
      + '<div data-testid="evaluatecare-splash" data-task="' + (task || 'splash') + '"'
      + ' style="display:flex;flex-direction:column;height:100%;min-height:0;">'
      + '<div style="padding:0.67em 1em;background:#f0fffe;border-bottom:0.125em solid #d8e2e1;flex-shrink:0;">'
      + '<div style="font-size:1em;color:#0b7a75;font-weight:600;">EvaluateCare</div>'
      + '</div>'
      + '<div style="flex:1;min-height:0;overflow:auto;padding:1em;">'
      + html
      + '</div>'
      + '</div>';
    if (target && typeof target.innerHTML !== 'undefined') {
      target.innerHTML = wrapped;
    }
    return wrapped;
  }
  window.EvaluateCareSplashRenderer = EvaluateCareSplashRenderer;
})();
