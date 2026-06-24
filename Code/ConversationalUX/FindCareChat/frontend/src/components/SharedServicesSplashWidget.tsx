// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SharedServicesSplashWidget — on router:action 'goto_sharedservices' fires
// router:makeCall(op:'splash'). Subscribed to kind:'splash' broadcast;
// paints the user_object into frame_MainWindow.

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
      <div>Loading SharedServices…</div>
    </div>
  `
}

function buildSplashHtml(data: any): string {
  const identity = (data && data.identity) || {}
  const threads = (data && data.threads) || {}
  const utterances = Array.isArray(threads.utterances) ? threads.utterances : []
  const actions = Array.isArray(threads.actions) ? threads.actions : []
  const identRows = Object.keys(identity).filter(k => k !== 'OAuthIdentities').map(k =>
    `<tr><td style="padding:0.2em 0.5em;color:#0b7a75;font-weight:700;border:0.0625em solid #d8e2e1;">${_esc(k)}</td>
         <td style="padding:0.2em 0.5em;border:0.0625em solid #d8e2e1;">${_esc(JSON.stringify(identity[k]))}</td></tr>`
  ).join('')
  const utteranceList = utterances.slice(-10).map((u: any) =>
    `<li style="margin:0.3em 0;color:#374151;">${_esc((u && (u.text || u.body || u)) || '')}</li>`
  ).join('') || '<li style="color:#6b7280;">No utterances.</li>'
  const actionList = actions.slice(-10).map((a: any) =>
    `<li style="margin:0.3em 0;color:#374151;font-size:0.9em;">
       <strong>${_esc(a.tool_name || '')}</strong> @ ${_esc(a.at || '')}
     </li>`
  ).join('') || '<li style="color:#6b7280;">No actions.</li>'
  return `
    <div style="padding:1em 1.5em;font-family:system-ui,-apple-system,sans-serif;">
      <h3 style="color:#0b7a75;margin:0 0 0.5em 0;">SharedServices — User Object</h3>
      <h4 style="color:#0b7a75;margin:0.75em 0 0.3em 0;font-size:0.9em;">Identity</h4>
      <table style="width:100%;border-collapse:collapse;font-size:0.85em;"><tbody>${identRows || '<tr><td style="padding:0.3em;color:#6b7280;">empty</td></tr>'}</tbody></table>
      <h4 style="color:#0b7a75;margin:0.75em 0 0.3em 0;font-size:0.9em;">Recent utterances</h4>
      <ul style="margin:0;padding-left:1.5em;font-size:0.9em;">${utteranceList}</ul>
      <h4 style="color:#0b7a75;margin:0.75em 0 0.3em 0;font-size:0.9em;">Recent actions</h4>
      <ul style="margin:0;padding-left:1.5em;font-size:0.9em;">${actionList}</ul>
    </div>
  `
}

export default function SharedServicesSplashWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'splash',
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
      if (msg.type === 'router:action' && msg.action === 'goto_sharedservices') {
        postRender(buildLoadingHtml())
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'splash',
          payload: {},
          call_id: 'splash-' + Date.now(),
        }, '*')
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'splash') {
        postRender(buildSplashHtml(msg.data || {}))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
