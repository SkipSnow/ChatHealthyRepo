// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// FakeGoogleLoginWidget — local-only fake Google sign-in form.
//
// On the local stack, the OAuth /auth/google/start endpoint redirects
// the popup to /fake_google/auth (the SS-hosted stand-in for Google).
// That endpoint now redirects to the wrapper page with
//   ?fake_google_login=1&state=<state>&flow=<login|register>
// and index.html forwards those params to this iframe as
//   {type:'popup_params', params: {fake_google_login, state, flow}}.
// This widget reads the message, renders the fake sign-in form into
// frame_MainWindow, and submits credentials back to SS's
// /fake_google/submit endpoint — which redirects the popup to the real
// /auth/google/callback. All HTML lives here in React per the
// architecture.

import { useEffect } from 'react'

const TARGET = 'MainWindow'

function _esc(s: string): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function buildFakeLoginHtml(state: string, flow: string, ssOrigin: string): string {
  const registerChecked = flow === 'register' ? 'checked' : ''
  return `
    <div style="font-family:Arial,sans-serif;padding:2em;max-width:30em;margin:0 auto;color:#202124;">
      <div style="background:#fef7e0;border:0.0625em solid #f9ab00;padding:0.5em 1em;border-radius:0.25em;font-size:0.85em;margin-bottom:1.5em;">
        <strong>LOCAL FAKE</strong> &mdash; not Google. Served by SharedServices on localhost only.
      </div>
      <h1 style="font-size:1.2em;margin-bottom:1em;">Sign in as a ChatHealthy user</h1>
      <form method="POST" action="${_esc(ssOrigin)}/fake_google/submit" data-testid="fake-google-form">
        <input type="hidden" name="state" value="${_esc(state)}">
        <input type="hidden" name="flow"  value="${_esc(flow)}">
        <label for="email" style="display:block;margin:0.75em 0 0.25em;font-size:0.9em;">Email</label>
        <input type="email" name="email" id="email" data-testid="fake-google-email" required
               style="width:100%;padding:0.5em;box-sizing:border-box;border:0.0625em solid #999;border-radius:0.25em;">
        <label for="password" style="display:block;margin:0.75em 0 0.25em;font-size:0.9em;">Password</label>
        <input type="password" name="password" id="password" data-testid="fake-google-password" required
               style="width:100%;padding:0.5em;box-sizing:border-box;border:0.0625em solid #999;border-radius:0.25em;">
        <div style="margin:1em 0;">
          <label><input type="checkbox" name="create_account" data-testid="fake-google-register-checkbox" ${registerChecked}> Create a new account</label>
        </div>
        <button type="submit" data-testid="fake-google-submit-button"
                style="background:#0b7a75;color:#fff;border:none;border-radius:0.25em;padding:0.625em 1.25em;font-size:0.95em;cursor:pointer;margin-top:1em;">Submit</button>
      </form>
    </div>
  `
}

export default function FakeGoogleLoginWidget() {
  useEffect(() => {
    // index.html sends popup_params on a short retry cadence to ride out
    // the React mount window. The first message that arrives renders the
    // form; subsequent identical messages are a no-op so user input is
    // not wiped by a re-render.
    let rendered = false
    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type !== 'popup_params') return
      const p = msg.params || {}
      if (p.fake_google_login !== '1') return
      if (rendered) return
      const state = String(p.state || '')
      const flow  = String(p.flow || 'login')
      // ssOrigin arrives in the popup_params envelope. The iframe is at
      // FindCare's origin so the wrapper's `_envServiceUrls` global is
      // not directly readable across origins.
      const env = (msg.env || {}) as { sharedservices?: string }
      const ssOrigin = env.sharedservices || ''
      rendered = true
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: false,
        content: buildFakeLoginHtml(state, flow, ssOrigin),
      }, '*')
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
