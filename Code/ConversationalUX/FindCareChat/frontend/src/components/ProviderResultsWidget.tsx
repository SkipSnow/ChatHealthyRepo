// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ProviderResultsWidget — subscribes to the SS gate broadcast for
// kind:'providers' and paints the provider list into frame_MainWindow
// via router:render. Also paints a "Searching..." indicator on submit.

import { useEffect } from 'react'

const TARGET = 'MainWindow'

function _esc(s: any): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

// renderSearching ported verbatim from prod _oneshots/prod_index.html
// lines 1791-1801. Flex-column centered layout; bare TEAL timer (no box);
// gray status footer. No invented styling.
function buildSearchingHtml(query: string, seconds: number): string {
  return `
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:1em;height:100%;">
      <div style="font-size:1em;color:#6b7280;">Searching for: <strong style="color:#374151;font-weight:600;">${_esc(query)}</strong></div>
      <div data-testid="searching-timer" style="font-size:1em;color:#0b7a75;font-weight:700;">${seconds}s</div>
      <div style="font-size:1em;color:#9ca3af;">Waiting for response...</div>
    </div>
  `
}

function buildProviderHtml(providers: any[], totalCount?: number, header?: string): string {
  if (!providers.length) {
    return `
      <div style="padding:2em;color:#6b7280;font-style:italic;">No providers matched.</div>
    `
  }
  const count = totalCount || providers.length
  const heading = header || `${count} provider${count === 1 ? '' : 's'} found`
  // renderProviderRow ported verbatim from prod _oneshots/prod_index.html
  // line 1819-1835. Action data-* attributes carry the full card payload
  // the SS provider_detail_tool requires.
  const rows = providers.map(p => {
    const name = _esc(p.name || '')
    const addrLine = _esc([p.address, p.city, p.state, p.zip].filter(Boolean).join(', '))
    const spec = _esc(p.specialty || p.primary_specialty || '')
    const detailAttrs =
      `data-router-action="provider:detail"` +
      ` data-npi="${_esc(p.npi || '')}"` +
      ` data-name="${_esc(p.name || '')}"` +
      ` data-specialty="${_esc(p.specialty || p.primary_specialty || '')}"` +
      ` data-address="${_esc(p.address || '')}"` +
      ` data-county="${_esc(p.county || '')}"` +
      ` data-phone="${_esc(p.phone || '')}"` +
      ` data-state="${_esc(p.state || '')}"`
    return (
      `<div data-testid="provider-card" data-npi="${_esc(p.npi || '')}" style="padding:0.5em 1em;border-bottom:0.0625em solid #eee;display:flex;justify-content:space-between;align-items:center;gap:1em;">` +
        `<div style="flex:1;min-width:0;">` +
          `<div style="font-weight:600;color:#0b7a75;">${name}</div>` +
          `<div style="font-size:0.9em;color:#6b7280;">${spec}</div>` +
          `<div style="font-size:0.9em;color:#9ca3af;">${addrLine}</div>` +
          `<div style="font-size:0.85em;color:#6b7280;">NPI: ${_esc(p.npi || '')}${p.phone ? ' &middot; Phone: ' + _esc(p.phone) : ''}${p.county ? ' &middot; County: ' + _esc(p.county) : ''}</div>` +
          `<div style="font-size:0.66em;margin-top:0.25em;"><a href="#" ${detailAttrs} style="color:#0b7a75;text-decoration:underline;">provider detail</a></div>` +
        `</div>` +
      `</div>`
    )
  }).join('')
  return (
    `<div style="display:flex;flex-direction:column;height:100%;min-height:0;">` +
      `<div style="padding:0.5em 1em;background:#f0fffe;border-bottom:0.125em solid #d8e2e1;color:#0b7a75;font-weight:600;">${_esc(heading)}</div>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;padding:1em;background:#fafafa;border-bottom:0.125em solid #eee;flex-shrink:0;">` +
        `<span style="font-weight:600;color:#0b7a75;text-transform:uppercase;">Available Providers</span>` +
        `<span style="color:#6b7280;">${providers.length} available</span>` +
      `</div>` +
      `<div data-testid="available-providers" style="flex:1;overflow:auto;">${rows}</div>` +
    `</div>`
  )
}

export default function ProviderResultsWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'providers',
    }, '*')

    let currentQuery = ''
    let startTs = 0
    let timer: ReturnType<typeof setInterval> | null = null

    function postRender(content: string) {
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: false,
        content,
      }, '*')
    }

    function stopTimer() {
      if (timer) { clearInterval(timer); timer = null }
    }

    function startTimer(query: string) {
      stopTimer()
      currentQuery = query
      startTs = Date.now()
      postRender(buildSearchingHtml(query, 0))
      timer = setInterval(function () {
        const sec = Math.round((Date.now() - startTs) / 1000)
        postRender(buildSearchingHtml(currentQuery, sec))
      }, 1000)
    }

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'router:action' && msg.action === 'user:submit') {
        const text = String((msg.data && msg.data.text) || '').trim()
        if (text) startTimer(text)
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'providers') {
        stopTimer()
        const data = msg.data || {}
        const providers = Array.isArray(data.providers) ? data.providers : []
        postRender(buildProviderHtml(providers, data.total_count, data.summary_message))
      }
    }
    window.addEventListener('message', onMessage)
    return () => { stopTimer(); window.removeEventListener('message', onMessage) }
  }, [])
  return null
}
