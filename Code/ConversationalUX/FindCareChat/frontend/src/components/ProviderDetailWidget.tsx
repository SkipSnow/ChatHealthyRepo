// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ProviderDetailWidget — on router:action 'provider:detail' fires
// router:makeCall(op:'provider-detail', payload:{npi}). Subscribed to
// kind:'provider-detail' broadcast; renders into frame_RightPanel.

import { useEffect } from 'react'

const TARGET = 'RightPanel'

function _esc(s: any): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function buildLoadingHtml(npi: string): string {
  return `
    <div style="padding:1em;font-family:system-ui,-apple-system,sans-serif;">
      <div style="color:#0b7a75;font-weight:700;font-size:0.95em;margin-bottom:0.4em;">Provider detail</div>
      <div style="color:#6b7280;font-size:0.85em;">Loading NPI ${_esc(npi)}…</div>
    </div>
  `
}

// SS provider_detail_tool response shape:
//   { provider_name, npi, identity:{primary_taxonomy_display,...},
//     addresses:[{line1,line2,city,state,zip,phone,address_type,...}],
//     licenses:[{state, number}], insurance:[], research_sites:{...} }
function buildDetailHtml(data: any): string {
  const p = data || {}
  const identity = p.identity || {}
  const name = _esc(p.provider_name || '')
  const specialty = _esc(identity.primary_taxonomy_display || '')
  const sect = (label: string, body: string) =>
    `<div style="margin-top:0.75em;">
       <div style="font-size:0.8em;font-weight:700;color:#0b7a75;text-transform:uppercase;letter-spacing:0.05em;">${_esc(label)}</div>
       <div style="font-size:0.9em;color:#374151;margin-top:0.2em;">${body}</div>
     </div>`
  const addrs = (Array.isArray(p.addresses) ? p.addresses : [])
    .map((a: any, i: number) => {
      const label = a.address_type
        ? (String(a.address_type).charAt(0).toUpperCase() + String(a.address_type).slice(1))
        : (i === 0 ? 'Primary' : `Address ${i + 1}`)
      const line = [a.line1, a.line2, a.city, a.state, a.zip].filter(Boolean).map(_esc).join(', ')
      const tail = a.phone ? `Phone: ${_esc(a.phone)}` : ''
      return `<div style="margin:0.3em 0;">
                <span style="font-weight:600;">${_esc(label)}:</span> ${line}
                ${tail ? `<div style="font-size:0.85em;color:#6b7280;">${tail}</div>` : ''}
              </div>`
    }).join('') || `<div style="color:#6b7280;font-style:italic;">No address on file.</div>`
  const licenses = (Array.isArray(p.licenses) ? p.licenses : [])
    .map((l: any) => `${_esc(l.state || '')} ${_esc(l.number || '')}`.trim())
    .filter(Boolean).join(' &middot; ') || 'None on file.'
  const insurance = (Array.isArray(p.insurance) ? p.insurance : [])
    .map((ins: any) => _esc(typeof ins === 'string' ? ins : (ins.payer || ins.identifier || '')))
    .filter(Boolean).join(', ') || 'None on file.'
  return `
    <div style="padding:0.75em 1em;">
      <div style="font-weight:700;color:#0b7a75;font-size:1.05em;">${name}</div>
      <div style="font-size:0.85em;color:#6b7280;">NPI: ${_esc(p.npi || '')}</div>
      <div style="font-size:0.85em;color:#374151;margin-top:0.2em;">${specialty}</div>
      ${sect('Practice addresses', addrs)}
      ${sect('Licenses', licenses)}
      ${sect('Insurance', insurance)}
    </div>
  `
}

export default function ProviderDetailWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'provider-detail',
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
      if (msg.type === 'router:action' && msg.action === 'provider:detail') {
        const d = msg.data || {}
        const npi  = String(d.npi  || '').trim()
        const name = String(d.name || '').trim()
        if (!npi || !name) return
        postRender(buildLoadingHtml(npi))
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'provider-detail',
          payload: {
            npi, name,
            specialty: d.specialty || null,
            address:   d.address   || null,
            county:    d.county    || null,
            phone:     d.phone     || null,
            state:     d.state     || null,
          },
          call_id: 'pd-' + Date.now(),
        }, '*')
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'provider-detail') {
        postRender(buildDetailHtml(msg.data || {}))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
