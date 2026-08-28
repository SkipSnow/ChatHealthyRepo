// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SessionDataWidget — on router:action 'goto_sharedservices' fires
// router:makeCall(op:'splash'). Subscribed to kind:'splash' broadcast;
// paints the user_object into frame_MainWindow.

import { useEffect } from 'react'

// Its own popup, beside About rather than painted over MainWindow.
const TARGET = 'SessionInfoPopUp'
// The element the PDF is taken from. Named here because the widget authors
// it; ClientRouter is handed the id and prints what it is given.
const SESSION_PRINT_ROOT = 'session_print_root'

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

// Ported verbatim from the prod splash renderer
// (Website/splash_render.js + Website/splash.css before the architecture
// rewrite). Identity = ordered dl/dt/dd grid. Session Conversation
// History = two side-by-side threads (Utterances + Actions), each with
// max-height:15em and overflow-y:auto so they scroll independently —
// the two scrollbars that make the live transcript easy to read.

const IDENTITY_ORDER = [
  'user_type', 'guid', 'origin', 'server_env', 'created_at', 'expires_at',
]
const IDENTITY_HIDDEN = new Set(['token', 'signature'])

function _fmt(v: any): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') {
    try { return JSON.stringify(v, null, 2) } catch { return String(v) }
  }
  return String(v)
}

function buildIdentityHtml(identity: any): string {
  const ordered: string[] = []
  const seen = new Set<string>()
  for (const k of IDENTITY_ORDER) {
    if (k in identity) { ordered.push(k); seen.add(k) }
  }
  for (const k of Object.keys(identity)) {
    if (seen.has(k) || IDENTITY_HIDDEN.has(k)) continue
    ordered.push(k)
  }
  const rows = ordered.map(k =>
    `<dt style="font-weight:600;color:#374151;">${_esc(k)}</dt>` +
    `<dd style="margin:0;color:#1f2937;white-space:pre-wrap;word-break:break-all;">${_esc(_fmt(identity[k]))}</dd>`
  ).join('')
  return `
    <section style="margin-top:1em;">
      <h3 style="margin:0 0 0.5em 0;color:#0b7a75;">Identity</h3>
      <dl style="margin:0;display:grid;grid-template-columns:16.25em 1fr;gap:0.25em 1em;">${rows}</dl>
    </section>
  `
}

// Deployment Facts — the build every server carries, two ways. `deployed`
// is what the deploy recorded; `running` is what the server says when it is
// asked. They are shown side by side because the week they disagreed was
// the week a Space reported one build while executing the code of another,
// and a single number cannot show that.
function buildDeploymentFactsHtml(rows: any[]): string {
  if (!Array.isArray(rows) || !rows.length) {
    return `
      <section style="margin-top:1em;">
        <h3 style="margin:0 0 0.25em 0;color:#0b7a75;">Deployment Facts</h3>
        <p style="margin:0;color:#9ca3af;font-style:italic;">No deployment on record for this environment.</p>
      </section>
    `
  }
  const head = ['Server', 'Package', 'Deployed', 'Running', 'Commit']
    .map(h => `<th style="text-align:left;padding:0.25em 0.75em 0.25em 0;color:#374151;border-bottom:0.125em solid #d1fae5;">${_esc(h)}</th>`)
    .join('')
  const body = rows.map(r => {
    const deployed = r.deployed_build == null ? '—' : String(r.deployed_build)
    const running  = r.error ? `unreachable`
                   : r.reported_build == null ? '—' : String(r.reported_build)
    const disagree = !r.error
      && r.deployed_build != null && r.reported_build != null
      && String(r.deployed_build) !== String(r.reported_build)
    const runColor = r.error ? '#d97706' : disagree ? '#b91c1c' : '#1f2937'
    const note = disagree ? ' ≠ deployed' : ''
    const title = r.error ? ` title="${_esc(r.error)}"` : ''
    return `<tr>
      <td style="padding:0.25em 0.75em 0.25em 0;color:#1f2937;">${_esc(r.target_id)}</td>
      <td style="padding:0.25em 0.75em 0.25em 0;color:#1f2937;">${_esc(r.package_id)}</td>
      <td style="padding:0.25em 0.75em 0.25em 0;color:#1f2937;">${_esc(deployed)}</td>
      <td style="padding:0.25em 0.75em 0.25em 0;color:${runColor};"${title}>${_esc(running + note)}</td>
      <td style="padding:0.25em 0.75em 0.25em 0;color:#6b7280;">${_esc(r.reported_commit || '')}</td>
    </tr>`
  }).join('')
  return `
    <section style="margin-top:1em;">
      <h3 style="margin:0 0 0.25em 0;color:#0b7a75;">Deployment Facts</h3>
      <p style="color:#6b7280;margin:0 0 0.5em 0;">What the deploy recorded, beside what each server says it is running.</p>
      <div style="overflow-x:auto;">
        <table style="border-collapse:collapse;width:100%;"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
      </div>
    </section>
  `
}

const PARAMETER_ORDER = [
  'geography', 'complaint', 'specialties', 'selected_specialty_codes',
]

function fmtParameter(name: string, value: any): string {
  if (value === null || value === undefined) return '(not set)'
  if (name === 'geography') {
    const parts = ['state', 'city', 'county', 'zip']
      .filter(k => value[k]).map(k => `${k}: ${value[k]}`)
    return parts.length ? parts.join(', ') : '(not set)'
  }
  if (name === 'specialties') {
    if (!Array.isArray(value) || !value.length) return '(none offered yet)'
    return value.map((s: any) =>
      `${s.name || '?'} (${s.code})${s.can_prescribe ? ' Rx' : ''}${s.homeopathic ? ' homeo' : ''}`
    ).join(', ')
  }
  if (name === 'selected_specialty_codes') {
    if (!Array.isArray(value) || !value.length) return '(nothing narrowed - all offered apply)'
    return value.join(', ')
  }
  if (typeof value === 'string') return value.trim() || '(not set)'
  return _fmt(value)
}

function buildParametersHtml(parameters: any): string {
  const params = parameters || {}
  const names = PARAMETER_ORDER.filter(k => k in params)
    .concat(Object.keys(params).filter(k => !PARAMETER_ORDER.includes(k)))
  const rows = names.map(k =>
    `<dt style="font-weight:600;color:#374151;">${_esc(k)}</dt>` +
    `<dd style="margin:0;color:#1f2937;white-space:pre-wrap;overflow-wrap:anywhere;">${_esc(fmtParameter(k, params[k]))}</dd>`
  ).join('')
  const body = rows
    ? `<dl style="margin:0;display:grid;grid-template-columns:16.25em 1fr;gap:0.25em 1em;">${rows}</dl>`
    : `<p style="margin:0;color:#9ca3af;font-style:italic;">Nothing set yet.</p>`
  return `
    <section style="margin-top:1em;">
      <h3 style="margin:0 0 0.25em 0;color:#0b7a75;">User Parameters</h3>
      <p style="color:#6b7280;margin:0 0 0.5em 0;">What is true right now, whichever tool set it. Live state, not history.</p>
      ${body}
    </section>
  `
}

function buildRow(at: any, body: string, n: any): string {
  const nLabel = (n !== undefined && n !== null && n !== '')
    ? `<span style="color:#6b7280;">#${_esc(n)} </span>` : ''
  return `
    <div style="padding:0.4em 0.6em;border-top:0.125em solid #d1fae5;">
      <div style="color:#6b7280;line-height:1.1;">${nLabel}${_esc(at || '')}</div>
      <div style="color:#1f2937;line-height:1.2;overflow-wrap:anywhere;">${body}</div>
    </div>
  `
}

function buildUtteranceRow(it: any): string {
  const actor = _esc(it.actor || '?')
  const text = _esc(it.text || '')
  const body =
    `<strong style="color:${it.actor === 'system' ? '#d97706' : '#0b7a75'};">${actor}:</strong> ${text}`
  return buildRow(it.at, body, it.n)
}

function buildActionRow(it: any): string {
  let inJson = ''
  let outJson = ''
  try {
    if (it.input_json && Object.keys(it.input_json).length) {
      inJson = JSON.stringify(it.input_json)
      if (inJson.length > 400) inJson = inJson.slice(0, 400) + '…'
    }
  } catch { inJson = '' }
  try {
    if (it.output_json && Object.keys(it.output_json).length) {
      outJson = JSON.stringify(it.output_json)
      if (outJson.length > 400) outJson = outJson.slice(0, 400) + '…'
    }
  } catch { outJson = '' }
  const body =
    `<strong>${_esc(it.tool_name || '?')}</strong>` +
    (inJson ? ` (${_esc(inJson)})` : '') +
    (outJson ? ` → ${_esc(outJson)}` : '')
  return buildRow(it.at, body, it.n)
}

function buildThreadHtml(title: string, items: any[], formatter: (it: any) => string): string {
  const empty = !items || items.length === 0
  const body = empty
    ? `<div style="padding:0.5em 0.6em;color:#9ca3af;font-style:italic;">(no entries yet)</div>`
    : items.map(formatter).join('')
  return `
    <div style="border-left:0.25em solid #0b7a75;background:#f0fffe;padding:0.5em 0.6em;">
      <div style="font-weight:600;color:#0b7a75;margin-bottom:0.5em;text-transform:uppercase;letter-spacing:0.05em;">${_esc(title)}</div>
      <div style="max-height:15em;overflow-y:auto;padding-right:0.5em;">${body}</div>
    </div>
  `
}

function buildSessionHtml(data: any): string {
  const identity = (data && data.identity) || {}
  const threads = (data && data.threads) || {}
  const utterances = Array.isArray(threads.utterances) ? threads.utterances : []
  const actions    = Array.isArray(threads.actions)    ? threads.actions    : []
  const history = (threads.empty || (!utterances.length && !actions.length))
    ? `<p style="margin:0;color:#9ca3af;font-style:italic;">No utterances or actions yet. Type a prompt or click around to see this grow.</p>`
    : `<div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75em;">
         ${buildThreadHtml('Utterances', utterances, buildUtteranceRow)}
         ${buildThreadHtml('Actions',    actions,    buildActionRow)}
       </div>`
  return `
    <div id="${SESSION_PRINT_ROOT}" style="display:flex;flex-direction:column;height:100%;min-height:0;padding:1em;box-sizing:border-box;max-width:102.5em;margin:0 auto;text-align:left;line-height:1.35;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1em;">
        <h2 style="margin:0 0 0.5em 0;color:#0b7a75;">Shared Services — User Object</h2>
        <button type="button" data-router-action="session_pdf" data-print-omit="1"
          style="flex:none;background:#0b7a75;color:#fff;border:none;border-radius:0.3em;padding:0.4em 0.9em;cursor:pointer;font:inherit;">PDF</button>
      </div>
      <p style="color:#6b7280;margin:0 0 0.5em 0;">Live evidence that the entrance code completed. The cookie carries only the GUID; the user object lives in <code>admin.Sessions</code>.</p>
      ${buildIdentityHtml(identity)}
      ${buildDeploymentFactsHtml((data && data.deployment_facts) || [])}
      ${buildParametersHtml((data && data.parameters) || {})}
      <section style="margin-top:1em;flex:1;min-height:0;display:flex;flex-direction:column;">
        <h3 style="margin:0 0 0.5em 0;color:#0b7a75;">Session Conversation History</h3>
        ${history}
      </section>
    </div>
  `
}

export default function SessionDataWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: 'router:subscribe-broadcast',
      kind: 'session_data',
    }, '*')

    function postRender(content: string) {
      window.parent.postMessage({
        type: 'router:render',
        target: TARGET,
        append: false,
        popup: true,
        content,
      }, '*')
    }

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'router:action' && msg.action === 'session_info') {
        postRender(buildLoadingHtml())
        window.parent.postMessage({
          type: 'router:makeCall',
          op: 'session_data',
          payload: {},
          call_id: 'session_data-' + Date.now(),
        }, '*')
        return
      }
      if (msg.type === 'router:action' && msg.action === 'session_pdf') {
        window.parent.postMessage({
          type: 'router:print',
          element: SESSION_PRINT_ROOT,
          title: 'ChatHealthy session ' + new Date().toISOString().slice(0, 19),
        }, '*')
        return
      }
      if (msg.type === 'router:event-broadcast' && msg.kind === 'session_data') {
        postRender(buildSessionHtml(msg.data || {}))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return null
}
