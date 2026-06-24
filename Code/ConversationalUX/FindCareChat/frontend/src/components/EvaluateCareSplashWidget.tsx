// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// EvaluateCareSplashWidget — on router:action 'goto_evaluatecare' fires
// router:makeCall(op:'evalcare-splash'). Subscribed to kind:'evalcare-splash'
// broadcast; paints into frame_MainWindow.

import { useEffect } from 'react'

const TARGET = 'MainWindow'

function _esc(s: any): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function buildLoadingHtml(): string {
  return `
    <div style="display:flex;align-items:center;justify-content:center;height:100%;padding:2em;color:#0b7a75;">
      <div>Loading EvaluateCare…</div>
    </div>
  `
}

function buildEvalcareSplashHtml(data: any): string {
  const html = String((data && (data.html || data.body || '')) || '').trim()
  if (html) {
    return `<div style="padding:1em 1.5em;">${html}</div>`
  }
  const pretty = _esc(JSON.stringify(data || {}, null, 2))
  return `
    <div style="padding:1em 1.5em;">
      <h3 style="color:#0b7a75;margin:0 0 0.5em 0;">EvaluateCare</h3>
      <pre style="background:#f5f5f4;padding:0.75em;border-radius:0.4em;font-size:0.8em;
                  white-space:pre-wrap;word-break:break-word;max-height:60vh;overflow:auto;">${pretty}</pre>
    </div>
  `
}

export default function EvaluateCareSplashWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'evalcare-splash',
    }, '*')

    function postRender(content: string) {
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: false,
        content,
      }, '*')
    }

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'router:action' && msg.action === 'goto_evaluatecare') {
        postRender(buildLoadingHtml())
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'evalcare-splash',
          payload: {},
          call_id: 'evalcare-' + Date.now(),
        }, '*')
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'evalcare-splash') {
        postRender(buildEvalcareSplashHtml(msg.data || {}))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
