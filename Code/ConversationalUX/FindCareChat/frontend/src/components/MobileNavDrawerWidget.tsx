// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// MobileNavDrawerWidget — on router:action 'toggle_mobile_nav' renders
// a drawer popup with the same nav buttons as the desktop header.

import { useEffect } from 'react'
import { openPopup, closePopup } from './popupFrame'

const TARGET = 'MobileNavDrawer'

function buildDrawerHtml(): string {
  return `
    <div style="display:flex;flex-direction:column;gap:0.5em;padding:0.5em 0;font-family:system-ui,-apple-system,sans-serif;">
      <button type="button" data-router-action="home_home"
              style="background:transparent;border:none;color:#6b7280;cursor:not-allowed;padding:0.5em 1em;text-align:left;">Home</button>
      <button type="button" data-router-action="oauth_start"
              style="background:#0b7a75;border:none;color:#fff;cursor:pointer;padding:0.5em 1em;border-radius:0.25em;text-align:left;">Login &amp; Registration</button>
      <button type="button" data-router-action="open_panel" data-path="products.html" data-title="Products &amp; Services"
              style="background:transparent;border:0.0625em solid #0b7a75;color:#0b7a75;cursor:pointer;padding:0.5em 1em;border-radius:0.25em;text-align:left;">Products &amp; Services</button>
    </div>
  `
}

export default function MobileNavDrawerWidget() {
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type !== 'router:action') return
      if (msg.action !== 'toggle_mobile_nav') return
      openPopup(TARGET, buildDrawerHtml())
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
