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
    var wrapped = '<div data-testid="evaluatecare-splash" data-task="' + (task || 'splash') + '">'
      + html + '</div>';
    if (target && typeof target.innerHTML !== 'undefined') {
      target.innerHTML = wrapped;
    }
    return wrapped;
  }
  window.EvaluateCareSplashRenderer = EvaluateCareSplashRenderer;
})();
