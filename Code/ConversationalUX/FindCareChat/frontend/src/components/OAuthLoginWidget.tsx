// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// OAuthLoginWidget — listens for router:action 'oauth_start'. Per
// architecture (slide 3), external auth flows like OAuth bypass the
// gateway unless audit is required. The login dance itself is ported
// verbatim from prod (_oneshots/prod_index.html lines 1067-1093): open
// a named popup, build a hidden form that POSTs session_guid + flow to
// SharedServices' /auth/google/start, target the popup, submit, remove.
// Executed in the parent window via router:exec so it has access to
// window._envServiceUrls and window._authBoot.

import { useEffect } from 'react'

const START_LOGIN_REGISTER_JS = `
(function (flow) {
  try {
    if (window._isSessionExpired && window._isSessionExpired()) {
      window._handleSessionExpired && window._handleSessionExpired();
      return;
    }
    var sharedUrl = (window._envServiceUrls || {}).sharedservices || 'https://localhost:8002';
    var sessionGuid = '';
    var tok = window._authBoot && window._authBoot.token;
    if (tok && tok.length >= 32) sessionGuid = tok.slice(-32);
    var popupName = 'chathealthy-oauth';
    window.open('about:blank', popupName,
      'width=520,height=680,resizable=yes,scrollbars=yes');
    var f = document.createElement('form');
    f.method = 'POST';
    f.action = sharedUrl + '/auth/google/start';
    f.target = popupName;
    f.style.display = 'none';
    var i1 = document.createElement('input');
    i1.type = 'hidden'; i1.name = 'session_guid'; i1.value = sessionGuid;
    f.appendChild(i1);
    var i2 = document.createElement('input');
    i2.type = 'hidden'; i2.name = 'flow';
    i2.value = (flow === 'register') ? 'register' : 'login';
    f.appendChild(i2);
    document.body.appendChild(f);
    f.submit();
    document.body.removeChild(f);
  } catch (_) { /* button must never throw */ }
})(__FLOW__);
`

export default function OAuthLoginWidget() {
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type !== 'router:action') return
      if (msg.action !== 'oauth_start') return
      const flow = (msg.data && msg.data.flow) === 'register' ? "'register'" : "'login'"
      window.parent.postMessage({
        type: 'router:exec',
        code: START_LOGIN_REGISTER_JS.replace('__FLOW__', flow),
      }, '*')
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
