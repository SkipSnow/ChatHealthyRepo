// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// HeaderWidget — renders Header frame chrome via ClientRouter.render.
// Per architecture spec (FrontEndArchitecture.pptx slide 3), all HTML is
// produced by React; index.html is the lightweight container.

import { useEffect } from 'react'

const TARGET = 'Header'

function buildHeaderHtml(): string {
  return `
    <style>
      .ch-header-nav-desktop { display: flex; }
      .ch-header-hamburger   { display: none; }
      @media (max-width: 720px) {
        .ch-header-nav-desktop { display: none; }
        .ch-header-hamburger   { display: inline-flex; }
      }
    </style>
    <div style="display:flex;align-items:center;gap:1.5em;padding:0.5em 1em;height:100%;box-sizing:border-box;">
      <a href="/" style="display:flex;align-items:center;gap:0.6em;text-decoration:none;">
        <span style="display:inline-flex;align-items:center;justify-content:center;
                     width:2.2em;height:2.2em;border-radius:50%;background:#0b7a75;color:#fff;">
          <svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg" width="1.2em" height="1.2em" fill="currentColor">
            <rect x="8" y="2" width="4" height="16" rx="1"/>
            <rect x="2" y="8" width="16" height="4" rx="1"/>
          </svg>
        </span>
        <span style="font-family:'DM Serif Display',serif;color:#0b7a75;font-size:1.1em;">
          Chat<span style="color:#1f2937;">Healthy</span>.ai
        </span>
      </a>
      <span style="font-family:'DM Serif Display',serif;font-style:italic;color:#e05a8a;
                   font-size:0.85em;transform:rotate(-8deg);display:inline-block;">Pre-Alpha</span>
      <nav class="ch-header-nav-desktop" style="margin-left:auto;gap:0.5em;font-size:0.9em;">
        <button type="button" data-router-action="home_home"
                style="background:transparent;border:none;color:#6b7280;cursor:not-allowed;padding:0.4em 0.8em;">Home</button>
        <button type="button" data-router-action="oauth_start"
                style="background:#0b7a75;border:none;color:#fff;cursor:pointer;padding:0.4em 0.8em;border-radius:0.25em;">Login &amp; Registration</button>
        <button type="button" data-router-action="open_panel" data-path="products.html" data-title="Products &amp; Services"
                style="background:transparent;border:0.0625em solid #0b7a75;color:#0b7a75;cursor:pointer;padding:0.4em 0.8em;border-radius:0.25em;">Products &amp; Services</button>
      </nav>
      <button type="button" class="ch-header-hamburger"
              data-router-action="toggle_mobile_nav"
              aria-label="Open menu"
              style="margin-left:auto;background:transparent;border:none;cursor:pointer;
                     padding:0.4em;align-items:center;justify-content:center;">
        <svg viewBox="0 0 22 22" width="1.8em" height="1.8em" fill="none" stroke="#0b7a75" stroke-width="2" stroke-linecap="round">
          <line x1="3" y1="6"  x2="19" y2="6"  />
          <line x1="3" y1="11" x2="19" y2="11" />
          <line x1="3" y1="16" x2="19" y2="16" />
        </svg>
      </button>
    </div>
  `
}

// Banner colors are 30% lighter than the prior teal/red so the post-login
// state reads as informational. Close button uses data-router-action
// 'oauth_banner_close' so ClientRouter binds its click; the action lands
// back on HeaderWidget and triggers the normal-header repaint.
function buildAuthBannerHtml(outcome: string, message: string): string {
  const bg = outcome === 'success' ? '#54a29e' : '#e76b6b'
  const safe = String(message || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return `
    <div style="display:flex;align-items:center;justify-content:center;gap:1em;height:100%;
                background:${bg};color:#fff;padding:0 1.5em;font-weight:600;font-size:1em;
                box-shadow:0 0.125em 0.25em rgba(0,0,0,0.15);">
      <span>${safe}</span>
      <button type="button" data-router-action="oauth_banner_close"
              style="background:rgba(255,255,255,0.2);color:#fff;border:0.0625em solid #fff;
                     border-radius:0.25em;padding:0.3em 0.8em;cursor:pointer;font-size:0.9em;
                     font-weight:600;">close</button>
    </div>
  `
}

export default function HeaderWidget() {
  useEffect(() => {
    function paintNormal() {
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: false,
        content: buildHeaderHtml(),
      }, '*')
    }
    paintNormal()

    // Poll the server for any pending OAuth result the callback stashed
    // on user_object.pending_oauth_result. Tool returns JSON; React
    // builds the banner. Polling runs whenever the user has kicked off
    // the OAuth flow and stops once a result lands.
    let pollTimer: ReturnType<typeof setInterval> | null = null
    function startPolling() {
      if (pollTimer) return
      const started = Date.now()
      pollTimer = setInterval(() => {
        // 5-minute ceiling so a closed popup or stalled flow doesn't
        // leave a polling loop running forever.
        if (Date.now() - started > 5 * 60_000) {
          stopPolling()
          return
        }
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'claim_oauth_result',
          payload: {},
          call_id: 'oauth-poll-' + Date.now(),
        }, '*')
      }, 1000)
    }
    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    }
    // Subscribe to the kind:'oauth_result' broadcast that the
    // claim_oauth_result handler emits on each poll.
    window.parent.postMessage({
      type: 'router:subscribe-broadcast', kind: 'oauth_result',
    }, '*')

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'router:action' && msg.action === 'oauth_start') {
        startPolling()
        return
      }
      // Two paths for the oauth_result event: subscribe-broadcast (the
      // wrapper-side subscription registered above) and router:event
      // (the per-getStreamedPayloads event stream). If the subscribe
      // registration raced the iframe load we'd miss the broadcast — the
      // router:event fallback catches the same data and is forwarded to
      // every getStreamedPayloads caller unconditionally.
      const isOauthResult =
        (msg.type === 'router:event-broadcast' && msg.kind === 'oauth_result') ||
        (msg.type === 'router:event' && msg.evt && msg.evt.kind === 'oauth_result')
      if (isOauthResult) {
        const payload = msg.type === 'router:event' ? msg.evt.data : msg.data
        const result = (payload && payload.result) || null
        if (result) {
          stopPolling()
          window.parent.postMessage({
            type: 'router:render',
            target: TARGET,
            append: false,
            popup: false,
            content: buildAuthBannerHtml(result.outcome, result.message),
          }, '*')
        }
        return
      }
      if (msg.type === 'router:action' && msg.action === 'oauth_banner_close') {
        paintNormal()
      }
    }
    window.addEventListener('message', onMessage)
    return () => {
      stopPolling()
      window.removeEventListener('message', onMessage)
    }
  }, [])
  return null
}
