// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// WelcomeWidget — fetches FindCare's /welcome HTML (the Georgia-2em
// welcome line) and wraps it in prod's chat-bubble verbatim
// (_oneshots/prod_index.html lines 1776-1781): outer
// `padding:1em;max-width:50em;margin:1em auto` + inner
// `padding:1em;border-radius:2.25em 2.25em 2.25em 0.5em;background:#fff;
// border:0.125em solid #e5e7eb;font-size:1em;line-height:1.6`.
// Result: bubble near top of frame_MainWindow with the centered welcome
// text inside.

import { useEffect } from 'react'

const TARGET = 'MainWindow'

const FALLBACK_WELCOME_HTML =
  '<div style="margin-top:10vh;">' +
    '<p style="text-align:center;font-family:Georgia,\'Times New Roman\',serif;font-size:2em;line-height:1.3;margin:0;">' +
      'Find care in the US &amp; clinical trials globally,<br>' +
      "Let's talk about it." +
    '</p>' +
  '</div>'

function wrapBubble(innerHtml: string): string {
  return `
    <div style="padding:1em;max-width:50em;margin:1em auto;">
      <div style="padding:1em;border-radius:2.25em 2.25em 2.25em 0.5em;background:#fff;border:0.125em solid #e5e7eb;font-size:1em;line-height:1.6;">
        ${innerHtml}
      </div>
    </div>
  `
}

export default function WelcomeWidget() {
  useEffect(() => {
    // The React iframe is at FindCare origin; window.location.origin is
    // that origin, and `/welcome` is served by the same FindCare backend.
    // Same-origin fetch — no cross-origin parent access required.
    const fcOrigin = window.location.origin
    let cachedHtml = FALLBACK_WELCOME_HTML

    // OAuth popup case: index.html forwards URL params via the
    // popup_params message, and FakeGoogleLoginWidget paints the
    // sign-in form into MainWindow. If WelcomeWidget paints first
    // (and then again on /welcome fetch resolve) the form gets
    // overwritten. Suppress all WelcomeWidget paints in popup mode.
    let suppressed = false

    function paint(inner: string) {
      if (suppressed) return
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: false,
        content: wrapBubble(inner),
      }, '*')
    }
    paint(FALLBACK_WELCOME_HTML)
    fetch(`${fcOrigin}/welcome`, { method: 'POST' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const html = d && typeof d.message === 'string' ? d.message : ''
        if (html) { cachedHtml = html; paint(html) }
      })
      .catch(() => {})

    // Re-paint the welcome bubble in two cases:
    //   1. UM closes the turn with target_action=closeConnection200 (e.g.
    //      asking the user for a refinement). The searching indicator
    //      in frame_MainWindow becomes stale; nothing else writes to
    //      that frame on the close path. Repaint so the user can read
    //      the prompt in frame_UserMessage against a clean MainWindow.
    //   2. The user clicks the FindCare nav button in the About popup
    //      (or anywhere else that fires router:action 'goto_findcare').
    window.parent.postMessage({ type: 'router:subscribe-broadcast', kind: 'close' }, '*')
    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'popup_params' && msg.params && msg.params.fake_google_login === '1') {
        suppressed = true
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'close') {
        paint(cachedHtml)
      } else if (msg.type === 'router:action' && msg.action === 'goto_findcare') {
        paint(cachedHtml)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
