// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// React widget that consumes the AboutChatHealthyTool's structured
// output and renders HTML, then hands the HTML to the wrapper's
// ClientRouter for placement in the popup target. POC of the new
// front-end architecture: tool → structured data → React widget →
// HTML → ClientRouter.

import { useEffect } from 'react'

const TARGET = 'AboutChatHealtyPopUP'

function _esc(s: any): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function buildAboutHtml(data: any): string {
  const company = _esc(data?.company_text || '')
  const bf = data?.build_facts || {}
  const sp = data?.security_panel || {}
  const factRow = (label: string, value: any) =>
    `<tr>
       <td style="padding:0.3em 0.6em;background:#f0fffe;color:#0b7a75;font-weight:700;border:0.0625em solid #d8e2e1;width:30%;">${_esc(label)}</td>
       <td style="padding:0.3em 0.6em;color:#374151;border:0.0625em solid #d8e2e1;">${_esc(value == null ? '—' : value)}</td>
     </tr>`
  const build = [
    factRow('Version', bf.version),
    factRow('Build', bf.build),
    factRow('Commit', bf.commit),
    factRow('Built at', bf.built_at),
    factRow('Environment', bf.env),
  ].join('')
  const security = [
    factRow('Server', sp.server),
    factRow('Environment', sp.env),
    factRow('Signed token', sp.signed_token),
    factRow('Nonce', sp.nonce),
    factRow('GUID', sp.guid),
    factRow('Verified', sp.verified),
    factRow('Time', sp.time),
  ].join('')

  return `
    <div style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;">
      <h2 style="margin:0 0 0.5em 0;color:#0b7a75;font-size:1.25em;">About ChatHealthy.ai</h2>
      <p style="margin:0 0 1em 0;line-height:1.55;">${company}</p>

      <h3 style="margin:1em 0 0.4em 0;color:#0b7a75;font-size:1em;border-bottom:0.125em solid #d8e2e1;padding-bottom:0.2em;">Build</h3>
      <table style="width:100%;border-collapse:collapse;font-size:0.95em;"><tbody>${build}</tbody></table>

      <div style="margin-top:1.25em;background:#f0fffe;border:0.25em solid #0b7a75;border-radius:0.4em;padding:0.75em;">
        <h3 style="margin:0 0 0.5em 0;color:#0b7a75;font-size:1em;">Security</h3>
        <table style="width:100%;border-collapse:collapse;font-size:0.95em;"><tbody>${security}</tbody></table>
      </div>
    </div>
  `
}

export default function AboutChatHealthyWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'about_chathealthy',
    }, '*')

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return

      if (msg.type === 'router:action' && msg.action === 'about_chathealthy') {
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'about_chathealthy',
          payload: {},
          call_id: 'about-' + Date.now(),
        }, '*')
        return
      }

      if (msg.type === 'router:event-broadcast' && msg.kind === 'about_chathealthy') {
        const html = buildAboutHtml(msg.data || {})
        window.parent.postMessage({
          type: 'router:render',
          target: TARGET,
          append: false,
          popup: true,
          content: html,
        }, '*')
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
