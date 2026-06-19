// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// FindCareApp — clean rebuild of the FindCare React frontend.
// Replaces ChatWindow.tsx tangled mess with clear separation of concerns.
//
// Layout after search:
//   ┌─────────────────────────────────┐
//   │ "fix my broken ankle in DE"     │  QuestionBar
//   ├─────────────────────────────────┤
//   │ provider cards (scrollable)     │  ProviderBrowser
//   │ << more >>                      │  pagination cursor
//   ├─────────────────────────────────┤
//   │ SELECTED FOR EVALUATION   1/5  │  SelectionBar (sticky)
//   │ DR. SMITH                  ✕   │
//   ├─────────────────────────────────┤
//   │ Type a message...        Send  │  InputBar
//   └─────────────────────────────────┘
//
// State:
//   - question: string (current search question)
//   - providers: Provider[] (from search API)
//   - selection: useSelectionState (available/selected/garbage)
//   - phase: 'welcome' | 'searching' | 'results'

import React, { useState, useRef, useCallback, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useSelectionState } from '@providers/useSelectionState'
import { ProviderCard } from '@providers/ProviderCard'
import type { Provider } from '@providers/provider'
import type { SpecialtyRecord } from '@findcare/SpecialtyFilter/useSpecialtyFilterController'

const API_URL = import.meta.env.VITE_API_URL ?? ''
const EVALCARE_URL = import.meta.env.VITE_EVALCARE_URL ?? ''

type Phase = 'welcome' | 'searching' | 'results' | 'error' | 'clarify'

// ── Utility: send postMessage to parent page ─────────────────────
function sendToParent(type: string, data: any = {}) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type, ...data }, '*')
  }
}

// ── EPIC-002-F-001-S-012-REQ-B-003: Check for security violation on every fetch ───
function checkSecurityViolation(resp: Response, url: string): void {
  if (resp.status === 403 || resp.status === 426) {
    throw new Error(`SECURITY: ${url} returned ${resp.status} — HTTPS required. HTTP calls are blocked.`)
  }
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} from ${url}`)
  }
}

// ── Session token ────────────────────────────────────────────────
// SharedServices is the sole auth-token (GUID) issuer. The chat iframe
// calls SS /auth/issue directly; the resulting signed token is carried on
// cross-component calls. No client-side fallback: a failed /auth/issue
// MUST surface as an error, never a placeholder.
function _sharedServicesUrl(apiUrl: string): string {
  // When VITE_API_URL is unset, apiUrl is '' and a relative fetch would hit
  // the iframe's own origin (FindCare). Fall back to window.location.origin
  // to derive a usable base, then point at SharedServices' port/host.
  const base = apiUrl || (typeof window !== 'undefined' ? window.location.origin : '')
  // Legacy old-style HF Space naming (kept for back-compat).
  if (base.includes('find-care-chat')) {
    return base.replace('find-care-chat', 'shared-services')
  }
  // Current HF Space naming: skipsnow-{prefix}chathealthyspace.hf.space ->
  // skipsnow-{prefix}sharedservicesspace.hf.space. Prefix is "", "dev-",
  // or "qa-". Hostname-only replace so we don't accidentally collide
  // with a path segment.
  if (base.includes('chathealthyspace.hf.space')) {
    return base.replace('chathealthyspace.hf.space', 'sharedservicesspace.hf.space')
  }
  // Local: SS on :8002 regardless of incoming port (FC :7860, Caddy :443).
  if (base.includes('localhost')) {
    return base.replace(/(\/\/localhost)(:\d+)?/, '$1:8002')
  }
  return base
}
let _sessionToken: any = null
function _requestParentAuth(timeoutMs: number): Promise<any> {
  return new Promise(resolve => {
    if (typeof window === 'undefined' || window.parent === window) { resolve(null); return }
    const handler = (e: MessageEvent) => {
      if (e.data && e.data.type === 'auth:boot' && e.data.token) {
        window.removeEventListener('message', handler)
        resolve(e.data.token)
      }
    }
    window.addEventListener('message', handler)
    try { window.parent.postMessage({type: 'auth:boot-request'}, '*') } catch { }
    setTimeout(() => { window.removeEventListener('message', handler); resolve(null) }, timeoutMs)
  })
}
async function getSessionToken(): Promise<any> {
  if (_sessionToken) return _sessionToken
  // The page-session GUID belongs to the parent. Per S-012-REQ-T-003 the
  // GUID is stable per session, so the iframe MUST share the parent's —
  // never mint independently. Standalone iframe runs are not supported.
  if (typeof window === 'undefined' || window.parent === window) {
    const ssUrl = _sharedServicesUrl(API_URL)
    const resp = await fetch(`${ssUrl}/auth/issue`, { method: 'POST' })
    checkSecurityViolation(resp, `${ssUrl}/auth/issue`)
    _sessionToken = await resp.json()
    return _sessionToken
  }
  const parentTok = await _requestParentAuth(10000)
  if (!parentTok) {
    throw new Error('iframe could not obtain auth:boot from parent within 10s')
  }
  _sessionToken = parentTok
  return parentTok
}
// Unsolicited auth:boot pushes from the parent ALWAYS overwrite — parent
// is the canonical source of the page-session GUID.
if (typeof window !== 'undefined' && window.parent !== window) {
  window.addEventListener('message', (e: MessageEvent) => {
    if (e.data && e.data.type === 'auth:boot' && e.data.token) {
      _sessionToken = e.data.token
    }
  })
}

// HuggingFace Spaces sleep after idle and return 503 on the first wake
// request. Retry once with a short backoff so transient infrastructure
// stalls don't surface as a fatal error. 500s (real server errors,
// including the entity_type fail-hard) are NOT retried.
async function fetchWithColdStartRetry(
  url: string, init: RequestInit, retries = 1, backoffMs = 2000,
): Promise<Response> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const resp = await fetch(url, init)
    if (resp.status !== 503 || attempt === retries) return resp
    await new Promise(r => setTimeout(r, backoffMs))
  }
  return fetch(url, init)
}

// ── Helper: render a system message with corrections marked red ──
// EPIC-002-F-010-S-001-REQ-B-011. Rule-008 statement 4 forbids regex in
// frontend code, so this walks the string with indexOf/substring.
function renderSystemMessageWithCorrections(
  text: string,
  corrections: Array<{original: string; corrected: string}>,
): React.ReactNode {
  if (!corrections.length) return text
  const parts: React.ReactNode[] = []
  let remaining = text
  let key = 0
  for (const c of corrections) {
    const marker = "(corrected from '" + c.original + "')"
    const idx = remaining.indexOf(marker)
    if (idx < 0) continue
    if (idx > 0) parts.push(<span key={key++}>{remaining.substring(0, idx)}</span>)
    parts.push(
      <span key={key++} style={{color: '#dc2626'}}>{marker}</span>
    )
    remaining = remaining.substring(idx + marker.length)
  }
  if (remaining) parts.push(<span key={key++}>{remaining}</span>)
  return <>{parts}</>
}


// ── Helper: render the shared "question bar" that sits above any
// search-result region. EPIC-006-F-031-S-002 / EPIC-006-F-002:
// reused for both provider results and clinical-trials results so the
// banner does NOT care which intent ran. Pass an optional rightLabel
// (e.g. "726 providers found", "12 clinical trials") to show on the
// right; omit for non-results clarification screens.
function renderQuestionBanner(
  question: string,
  corrections: Array<{original: string; corrected: string}>,
  rightLabel?: string,
): React.ReactNode {
  return (
    <div style={{
      padding: '1em', background: '#f0fffe', borderBottom: '0.25em solid #0b7a75',
      fontSize: '1em', color: '#0b7a75', fontWeight: 600,
    }}>
      {renderQuestionWithCorrections(question, corrections)}
      {rightLabel && (
        <span style={{ float: 'right', fontWeight: 400, color: '#6b7280', fontSize: '1em' }}>
          {rightLabel}
        </span>
      )}
    </div>
  )
}


// ── Helper: format ONE trial as the full center-panel detail view.
// EPIC-006-F-031 — every V7 in-scope attribute that has a value is
// surfaced. Top section is a 4-col label/value table; sites are a
// 3-col table (label + 2 site columns, sites paired 2 per row); the
// rest are sectioned markdown blocks.
function _md(v: any): string {
  if (v === null || v === undefined) return '—'
  const s = String(v).trim()
  return s.length === 0 ? '—' : s.replace(/\|/g, '\\|')
}
function _yn(v: any): string { return v ? 'yes' : 'no' }
function _join(arr: any, sep = ', '): string {
  if (!Array.isArray(arr) || arr.length === 0) return ''
  return arr.filter(Boolean).join(sep)
}
function _kv4(rows: Array<[string, any, string, any]>): string {
  const header = '| Label | Value | Label | Value |\n|---|---|---|---|'
  const body = rows.map(r => `| ${_md(r[0])} | ${_md(r[1])} | ${_md(r[2])} | ${_md(r[3])} |`).join('\n')
  return header + '\n' + body
}
function _outcomeTable(outcomes: any[]): string {
  if (!outcomes || !outcomes.length) return ''
  const header = '| Measure | Description | Time frame |\n|---|---|---|'
  const body = outcomes.map((o: any) =>
    `| ${_md(o.measure)} | ${_md(o.description)} | ${_md(o.time_frame)} |`,
  ).join('\n')
  return header + '\n' + body
}
function formatTrialDetail(trial: any): string {
  const title = trial.brief_title || '(no title)'
  const phases = _join(trial.phases) || '—'
  const conds  = _join(trial.conditions) || '—'
  const kw     = _join(trial.keywords)

  // Top — 4-col label/value table (Skip req #3)
  const topRows: Array<[string, any, string, any]> = [
    ['National Clinical Trial ID', trial.nct_id,      'Status',         trial.overall_status],
    ['Phase',       phases,                    'Enrollment',     trial.enrollment_count],
    ['Study type',  trial.study_type,          'Allocation',     trial.design_info?.allocation],
    ['Sponsor',     trial.lead_sponsor_name,   'Sponsor type',   trial.lead_sponsor_class],
    ['Organization',trial.organization?.full_name, 'Org class',  trial.organization?.class],
    ['Start',       trial.start_date,          'Primary completion', trial.primary_completion_date],
    ['First submit',trial.study_first_submit_date, 'Last update',trial.last_update_post_date],
    ['Min age',     trial.minimum_age,         'Max age',        trial.maximum_age],
    ['Sex',         trial.sex,                 'Healthy vol.',   _yn(trial.healthy_volunteers)],
    ['DMC oversight', _yn(trial.oversight_has_dmc), 'FDA-regulated drug', _yn(trial.is_fda_regulated_drug)],
    ['FDA-regulated device', _yn(trial.is_fda_regulated_device), 'Unapproved device', _yn(trial.is_unapproved_device)],
    ['Pediatric postmarket study (PPSD)', _yn(trial.is_ppsd), 'FDAAA 801 violation', _yn(trial.fdaaa_801_violation)],
  ]

  // Design block — table form, all the design_info attrs
  const di = trial.design_info || {}
  const designRows: Array<[string, any, string, any]> = [
    ['Allocation',           di.allocation,            'Intervention model',  di.intervention_model],
    ['Primary purpose',      di.primary_purpose,       'Observational model', di.observational_model],
    ['Time perspective',     di.time_perspective,      'Masking',             di.masking],
    ['Who masked',           _join(di.who_masked),     'Target duration',     trial.target_duration],
    ['Bio-spec retention',   trial.bio_spec?.retention,'Expanded access',     _join(trial.expanded_access_types)],
  ]
  const designIntDesc = di.intervention_model_description ? `**Intervention model description:** ${di.intervention_model_description}` : ''
  const designMaskDesc = di.masking_description ? `**Masking description:** ${di.masking_description}` : ''
  const bioSpecDesc = trial.bio_spec?.description ? `**Bio-spec description:** ${trial.bio_spec.description}` : ''

  // Responsible party
  const rp = trial.responsible_party || {}
  const respPartyRows: Array<[string, any, string, any]> = [
    ['Type',                 rp.type,                  'Investigator',        rp.investigator_full_name],
    ['Investigator title',   rp.investigator_title,    'Investigator affil.', rp.investigator_affiliation],
  ]
  const hasRespParty = rp.type || rp.investigator_full_name || rp.investigator_title || rp.investigator_affiliation

  // Collaborators
  const collaborators = (trial.collaborators || []).map((c: any) =>
    `- **${_md(c.name)}** (${_md(c.class)})`,
  ).join('\n')

  // Secondary IDs
  const secIds = (trial.secondary_id_infos || []).map((s: any) =>
    `- ${_md(s.id)} — ${_md(s.type)}${s.domain ? ' / ' + _md(s.domain) : ''}`,
  ).join('\n')

  // Arms
  const arms = (trial.arm_groups || []).map((a: any) => {
    const ints = _join(a.intervention_names)
    return [
      `**${_md(a.label)}** _(${_md(a.type)})_`,
      a.description ? a.description : '',
      ints ? `Interventions: ${ints}` : '',
    ].filter(Boolean).join('  \n')
  }).join('\n\n')

  // Interventions
  const interventions = (trial.interventions || []).map((i: any) => {
    return [
      `**${_md(i.type)}: ${_md(i.name)}**`,
      i.description ? i.description : '',
      i.arm_group_labels?.length ? `Arms: ${_join(i.arm_group_labels)}` : '',
      i.other_names?.length ? `Other names: ${_join(i.other_names)}` : '',
    ].filter(Boolean).join('  \n')
  }).join('\n\n')

  // Outcomes
  const primaryOutcomes  = _outcomeTable(trial.primary_outcomes || [])
  const secondaryOutcomes = _outcomeTable(trial.secondary_outcomes || [])
  const otherOutcomes    = _outcomeTable(trial.other_outcomes || [])

  // Eligibility
  const eligRows: Array<[string, any, string, any]> = [
    ['Sex',                  trial.sex,                'Min age',             trial.minimum_age],
    ['Max age',              trial.maximum_age,        'Healthy volunteers',  _yn(trial.healthy_volunteers)],
    ['Gender description',   trial.gender_description, 'Std ages',            ''],
  ]
  const eligCriteria = trial.eligibility_criteria ? `**Criteria:**\n\n${trial.eligibility_criteria}` : ''

  // Contacts
  const centralContacts = (trial.central_contacts || []).map((c: any) => {
    const phone = c.phone ? `📞 ${c.phone}${c.phone_ext ? ' x' + c.phone_ext : ''}` : ''
    const email = c.email ? `✉ ${c.email}` : ''
    const npi   = c.npi   ? ` [Provider record](/provider/${c.npi})` : ''
    return `- **${_md(c.name)}** _(${_md(c.role)})_ — ${[phone, email].filter(Boolean).join(' · ')}${npi}`
  }).join('\n')

  // Overall officials (includes PI / Study Director / Study Chair)
  const officials = (trial.overall_officials || []).map((o: any) =>
    `- **${_md(o.name)}** _(${_md(o.role)})_ — ${_md(o.affiliation)}`,
  ).join('\n')

  // Sites — 3-col table, all sites, paired 2 per row (Skip req #1+#4)
  // Column A = label (only first row says "Sites"), B = site 1, C = site 2.
  const sites = (trial.locations || []).map((l: any) => {
    const where = [l.facility, [l.city, l.state, l.zip].filter(Boolean).join(', '), l.country]
      .filter(Boolean).join(' — ')
    const status = l.status ? ` _(${l.status})_` : ''
    const travel = (l.distance && l.duration) ? `  · ${l.distance} · ${l.duration}` : ''
    return `${where}${status}${travel}`
  })
  let sitesTable = ''
  if (sites.length) {
    const rows: string[] = []
    for (let i = 0; i < sites.length; i += 2) {
      const label = i === 0 ? 'Sites' : ''
      const a = sites[i] || ''
      const b = sites[i + 1] || ''
      rows.push(`| ${_md(label)} | ${_md(a)} | ${_md(b)} |`)
    }
    sitesTable = '|  |  |  |\n|---|---|---|\n' + rows.join('\n')
  }

  // References / see-also
  const references = (trial.references || []).map((r: any) => {
    const pubmed = r.pmid ? ` [(PubMed)](https://pubmed.ncbi.nlm.nih.gov/${r.pmid}/)` : ''
    return `- _(${_md(r.type)})_ ${_md(r.citation)}${pubmed}`
  }).join('\n')
  const seeAlso = (trial.see_also_links || []).map((s: any) =>
    `- [${_md(s.label || s.url)}](${s.url})`,
  ).join('\n')

  // IPD sharing
  const ipd = trial.ipd_sharing || {}
  const hasIpd = ipd.ipd_sharing || ipd.description || ipd.access_criteria || ipd.url
  const ipdRows: Array<[string, any, string, any]> = [
    ['IPD sharing',          ipd.ipd_sharing,          'Time frame',          ipd.time_frame],
    ['Info types',           _join(ipd.info_types),    'URL',                 ipd.url],
  ]
  const ipdAccess = ipd.access_criteria ? `**Access criteria:** ${ipd.access_criteria}` : ''
  const ipdDesc   = ipd.description ? `**Description:** ${ipd.description}` : ''

  // MeSH-derived browse leaves
  const condLeaves = (trial.condition_browse_leaves || []).map((b: any) =>
    `- ${_md(b.name)}${b.relevance ? ` _(relevance: ${b.relevance.toLowerCase()})_` : ''}`,
  ).join('\n')
  const interventionLeaves = (trial.intervention_browse_leaves || []).map((b: any) =>
    `- ${_md(b.name)}${b.relevance ? ` _(relevance: ${b.relevance.toLowerCase()})_` : ''}`,
  ).join('\n')

  // Large docs
  const largeDocs = (trial.large_docs || []).map((d: any) => {
    const flags = [d.has_protocol && 'Protocol', d.has_sap && 'SAP', d.has_icf && 'ICF'].filter(Boolean).join(', ')
    return `- **${_md(d.label || d.filename)}**${flags ? ` _(${flags})_` : ''} — ${_md(d.date || d.upload_date)}`
  }).join('\n')

  // Violations
  const violations = (trial.violation_annotation?.violation_events || []).map((v: any) =>
    `- **${_md(v.type)}** — ${_md(v.description)} _(issued ${_md(v.issued_date)})_`,
  ).join('\n')

  // Compose
  return [
    `## ${title}`,
    trial.official_title && trial.official_title !== title ? `_${trial.official_title}_` : '',
    trial.acronym ? `**Acronym:** ${trial.acronym}` : '',
    _kv4(topRows),

    trial.brief_summary ? `### Brief summary\n\n${trial.brief_summary}` : '',
    trial.detailed_description ? `### Detailed description\n\n${trial.detailed_description}` : '',

    conds ? `### Conditions\n\n${conds}` : '',
    kw ? `**Keywords:** ${kw}` : '',

    '### Design',
    _kv4(designRows),
    designIntDesc, designMaskDesc, bioSpecDesc,

    hasRespParty ? '### Responsible party' : '',
    hasRespParty ? _kv4(respPartyRows) : '',

    collaborators ? `### Collaborators\n\n${collaborators}` : '',
    secIds ? `### Secondary IDs\n\n${secIds}` : '',

    arms ? `### Arms\n\n${arms}` : '',
    interventions ? `### Interventions\n\n${interventions}` : '',

    primaryOutcomes ? `### Primary outcomes\n\n${primaryOutcomes}` : '',
    secondaryOutcomes ? `### Secondary outcomes\n\n${secondaryOutcomes}` : '',
    otherOutcomes ? `### Other outcomes\n\n${otherOutcomes}` : '',

    '### Eligibility',
    _kv4(eligRows),
    eligCriteria,

    centralContacts ? `### Central contacts\n\n${centralContacts}` : '',
    officials ? `### Overall officials\n\n${officials}` : '',

    sitesTable ? `### Sites (${sites.length})\n\n${sitesTable}` : '',

    references ? `### References\n\n${references}` : '',
    seeAlso ? `### See also\n\n${seeAlso}` : '',

    hasIpd ? '### IPD sharing' : '',
    hasIpd ? _kv4(ipdRows) : '',
    ipdDesc, ipdAccess,

    condLeaves ? `### MeSH-mapped conditions\n\n${condLeaves}` : '',
    interventionLeaves ? `### MeSH-mapped interventions\n\n${interventionLeaves}` : '',

    largeDocs ? `### Study documents\n\n${largeDocs}` : '',
    violations ? `### Compliance violations\n\n${violations}` : '',

    trial.study_url ? `[View on ClinicalTrials.gov](${trial.study_url})` : '',
  ].filter(Boolean).join('\n\n')
}


// ── Helper: build the leftPanel HTML for the bullet-list of trials.
// EPIC-006-F-031 — Skip req #2: bullet per trial, NCT ID as the click
// target, four sub-bullets (conditions, distance to nearest site, age
// range, gender restrictions). Click semantics handled in the parent
// (Website/index.html) — each <a data-trial-index="N"> posts back a
// trial:select message so the iframe can switch its detail view.
function _escapeHtml(s: any): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}
function _nearestDistance(trial: any): string {
  const locs = trial.locations || []
  const withDist = locs.filter((l: any) => l.distance && l.duration)
  if (!withDist.length) return '—'
  // distance is a string like "236 miles"; pull the numeric prefix.
  const ranked = withDist.slice().sort((a: any, b: any) => {
    const na = parseFloat(String(a.distance).replace(/[^0-9.]/g, '')) || 0
    const nb = parseFloat(String(b.distance).replace(/[^0-9.]/g, '')) || 0
    return na - nb
  })
  return `${ranked[0].distance} · ${ranked[0].duration}`
}
function _ageRange(trial: any): string {
  const a = (trial.minimum_age || '').trim()
  const b = (trial.maximum_age || '').trim()
  if (!a && !b) return '—'
  if (a && b) return `${a} – ${b}`
  return a || b
}
function buildTrialsLeftPanelHtml(trials: any[]): string {
  const items = trials.map((t: any, i: number) => {
    const nct = _escapeHtml(t.nct_id || '(no id)')
    const title = _escapeHtml(t.brief_title || '')
    const conds = _escapeHtml((t.conditions || []).join(', ') || '—')
    const dist  = _escapeHtml(_nearestDistance(t))
    const ages  = _escapeHtml(_ageRange(t))
    const sex   = _escapeHtml(t.sex || '—')
    return `
      <li style="margin-bottom:0.75em;">
        <a href="#" data-trial-index="${i}"
           style="color:#0b7a75; font-weight:700; text-decoration:underline; cursor:pointer;">
          ${nct}
        </a>
        <div style="font-size:0.9em; color:#374151; margin:0.15em 0 0.25em 0;">${title}</div>
        <ul style="margin:0.25em 0 0 1em; padding:0; font-size:0.9em; color:#4b5563;">
          <li><strong>Conditions:</strong> ${conds}</li>
          <li><strong>Distance to nearest site:</strong> ${dist}</li>
          <li><strong>Age range:</strong> ${ages}</li>
          <li><strong>Sex:</strong> ${sex}</li>
        </ul>
      </li>`
  }).join('')
  return `
    <div style="height:100%; overflow-y:auto; padding:1em; box-sizing:border-box;">
      <div style="font-size:1em; font-weight:700; color:#0b7a75; text-transform:uppercase; margin-bottom:0.5em;">
        Clinical trials (${trials.length})
      </div>
      <ul style="list-style:disc; padding-left:1.25em; margin:0;">${items}</ul>
    </div>`
}


// ── Helper: render the user's original question with each misspelled
// word followed by a red "(corrected from '<original>')" annotation.
// Used in the question-bar header so the header carries the same
// correction signal as the system bubble. Case-insensitive search
// because the classifier may normalize the original word's case.
function renderQuestionWithCorrections(
  original: string,
  corrections: Array<{original: string; corrected: string}>,
): React.ReactNode {
  if (!corrections.length) return original
  let remaining = original
  const parts: React.ReactNode[] = []
  let key = 0
  for (const c of corrections) {
    const lowerRemaining = remaining.toLowerCase()
    const idx = lowerRemaining.indexOf(c.original.toLowerCase())
    if (idx < 0) continue
    const matched = remaining.substring(idx, idx + c.original.length)
    if (idx > 0) parts.push(<span key={key++}>{remaining.substring(0, idx)}</span>)
    parts.push(<span key={key++}>{c.corrected}</span>)
    parts.push(
      <span key={key++} style={{color: '#dc2626'}}>
        {" (corrected from '" + matched + "')"}
      </span>
    )
    remaining = remaining.substring(idx + c.original.length)
  }
  if (remaining) parts.push(<span key={key++}>{remaining}</span>)
  return <>{parts}</>
}


// ── Main Component ───────────────────────────────────────────────
export default function FindCareApp() {
  const [phase, setPhase] = useState<Phase>('welcome')
  const [question, setQuestion] = useState('')
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [systemMessage, setSystemMessage] = useState('')
  // EPIC-006-F-031 — clinical trials: list lives in state so the left
  // panel can drive selection and the center panel renders one trial.
  const [trialsList, setTrialsList] = useState<any[]>([])
  const [selectedTrialIndex, setSelectedTrialIndex] = useState<number>(0)
  const [systemCorrections, setSystemCorrections] = useState<Array<{original: string; corrected: string}>>([])
  const [welcomeHtml, setWelcomeHtml] = useState('')
  const [thinkSeconds, setThinkSeconds] = useState(0)
  const [totalCount, setTotalCount] = useState(0)
  const [searchParams, setSearchParams] = useState<any>(null)
  const [lastNpi, setLastNpi] = useState('')
  const [hasMore, setHasMore] = useState(false)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  // EPIC-006-F-002-S-001-REQ-B-001 (Apply-Filter middle-screen behavior):
  // during a filter-apply re-classify, the Available Providers area is
  // emptied + shows the main-screen timer; Selected-for-Evaluation and
  // the prompt row remain. Distinct from phase='searching' (initial search)
  // which has nothing to preserve.
  const [reclassifying, setReclassifying] = useState(false)
  const [tokenReady, setTokenReady] = useState<boolean>(_sessionToken != null)
  const [loadingSeconds, setLoadingSeconds] = useState(0)
  // When the server streams a kind:"prompt" mid-stream, we keep the
  // timer running and the Send button disabled so the user can't fire
  // a second /gate hit before the first one persists. The input field
  // re-enables so the user can pre-type their answer; only Send stays
  // gated until kind:"final" arrives.
  const [searchPromptUp, setSearchPromptUp] = useState(false)

  const selection = useSelectionState()
  const selectedRef = useRef<Provider[]>([])
  selectedRef.current = selection.state.selected
  const searchParamsRef = useRef<any>(null)
  const questionRef = useRef('')
  const specialtyMapRef = useRef<Record<string, string>>({})
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  // Bug 1 (blocking input): allow a new search to abort the previous in-flight
  // /classify + /search pair so the user can re-issue mid-flight without a
  // stale response stomping the new one.
  const searchAbortRef = useRef<AbortController | null>(null)

  // EPIC-006-F-002-S-001-REQ-B-001: SpecialtyFilter rows (the cached client
  // list — see "Cache Results on client" in REQ-B-001) and a live ref to
  // the codes the user currently has checked, so a parent-driven
  // filter-apply postMessage submits exactly that set.
  const [specialtyRows, setSpecialtyRows] = useState<SpecialtyRecord[]>([])
  const checkedCodesRef = useRef<string[]>([])

  useEffect(() => {
    fetch(`${API_URL}/welcome`, { method: 'POST' })
      .then(r => r.json())
      .then(d => setWelcomeHtml(d.message || 'Welcome to ChatHealthy FindCare'))
      .catch(() => setWelcomeHtml('Welcome to ChatHealthy FindCare'))
  }, [])

  useEffect(() => {
    if (tokenReady) return
    const secondsTick = setInterval(() => setLoadingSeconds(s => s + 1), 1000)
    const poll = setInterval(() => {
      if (_sessionToken != null) setTokenReady(true)
    }, 100)
    getSessionToken().catch(() => { /* parent never replied; poll keeps trying */ })
    return () => { clearInterval(secondsTick); clearInterval(poll) }
  }, [tokenReady])

  // Keep refs in sync for closure access
  searchParamsRef.current = searchParams
  questionRef.current = question

  // Listen for parent page events (filter apply, evaluate click)
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object') return

      // EPIC-006-F-031 — leftPanel bullet click → switch center detail
      if (msg.type === 'trial:select' && typeof msg.index === 'number') {
        setSelectedTrialIndex(msg.index)
      }

      // RestoreState=false — reset to welcome, focus input
      if (msg.type === 'gui:reset') {
        setPhase('welcome')
        setQuestion('')
        setInput('')
        setError('')
        selection.flushGarbage()
        setTimeout(() => inputRef.current?.focus(), 100)
      }

      if (msg.type === 'gui:event') {
        if (msg.action === 'filter-selection-change') {
          // Option B: filter sub-iframe reports user's current checked
          // codes; we cache them so the next filter-apply submits exactly
          // that set.
          if (Array.isArray(msg.codes)) {
            checkedCodesRef.current = msg.codes
          }
        }
        if (msg.action === 'filter-apply') {
          // EPIC-002-F-010-S-002-REQ-B-002: Apply Filter routes
          // through SharedServices /gate, NOT FindCare's /search.
          // UR reads the carried session geography and either:
          //   - dispatches ProviderSearch directly with the new codes
          //     (geography sufficient), or
          //   - dispatches UM as a manufacture-trigger (geography
          //     insufficient) which authors a context-sensitive
          //     location prompt and ends in closeConnection200.
          // EPIC-006-F-002-S-001-REQ-B-001 submission rule: the codes
          // submitted are the SpecialtyFilter's currently-checked
          // rows. Fall back to msg.value for back-compat with the
          // parent's legacy HTML panel until the parent stops sending
          // it.
          let codes: string[] = checkedCodesRef.current
          if (!codes.length && msg.value) {
            try { codes = JSON.parse(msg.value) } catch { codes = [] }
          }
          setReclassifying(true)
          doApplyFilter(codes).finally(() => {
            setReclassifying(false)
          })
        }
        if (msg.action === 'evaluate-providers') {
          handleEvaluate()
        }
      }
    }
    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [])

  // ── Search ─────────────────────────────────────────────────────
  // ── Search: one /gate stream, NDJSON consumer ───────────────────
  // The prompt's Send button funnels through SharedServices /gate. The
  // universal_navigation_tool routes the utterance, calls specialty_filter_tool
  // then provider_search_and_selection_tool server-side, and emits NDJSON
  // events as each stage completes. We render progressively.
  const doSearch = useCallback(async (text: string) => {
    if (searchAbortRef.current) searchAbortRef.current.abort()
    const ac = new AbortController()
    searchAbortRef.current = ac

    setQuestion(text)
    setPhase('searching')
    setThinkSeconds(0)
    setError('')
    setSystemMessage('')
    setSystemCorrections([])
    setTrialsList([])
    setSelectedTrialIndex(0)
    selection.flushGarbage()

    const start = Date.now()
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setThinkSeconds(Math.round((Date.now() - start) / 1000))
    }, 1000)

    const finishTimer = () => {
      if (timerRef.current) clearInterval(timerRef.current)
      timerRef.current = null
    }

    let sawError = false
    let sawTerminalEvent = false
    let sawPrompt = false
    let sawProviders = false
    let sawSpecialties = false

    const onPrompt = (data: any) => {
      const text = String(data?.text || '').trim()
      if (!text) return
      // Show the prompt to the user mid-stream, but DO NOT stop the timer
      // and DO NOT exit the 'searching' phase — the server is still
      // working (SpecialtyFilter, ProviderSearch, persist all run after
      // kind:"prompt"). Releasing Send here lets the user click again
      // before the prior turn persists, which produces a stale read on
      // the next /gate hit. Send stays disabled until kind:"final".
      // searchPromptUp re-enables the input field so the user can pre-type
      // their answer while the server finishes.
      console.log('[FindCare] stream event kind=prompt text=', text)
      setSystemMessage(text)
      setSystemCorrections(Array.isArray(data?.corrections) ? data.corrections : [])
      setSearchPromptUp(true)
      sawTerminalEvent = true
      sawPrompt = true
    }

    const onSpecialties = (data: any) => {
      console.log('[FindCare] stream event kind=specialties count=', data?.specialties?.length || 0)
      sawSpecialties = true
      if (data.error || !data.specialties?.length) {
        // No-match path — surface as a fatal so the parent wrapper renders
        // the full-screen overlay (same UX as today).
        finishTimer()
        const msg = data.error || 'Could not identify relevant specialties'
        sendToParent('gui:fatal-error', { message: msg })
        setError(msg)
        setPhase('error')
        sawError = true
        return
      }
      const specMap: Record<string, string> = {}
      data.specialties.forEach((s: any) => { specMap[s.code] = s.name })
      specialtyMapRef.current = specMap

      const codes = data.specialties.map((s: any) => s.code)
      const params: any = { nucc_codes: codes, limit: 25 }
      setSearchParams(params)

      const filterOptions = data.specialties.map((s: any) => ({
        code: s.code, name: s.name,
        can_prescribe: s.can_prescribe ?? true,
        homeopathic: s.homeopathic ?? false,
      }))
      const homeoGeneralists = (data.homeopathic_generalists || []).map((s: any) => ({
        code: s.code, name: s.name,
        can_prescribe: s.can_prescribe ?? false,
        homeopathic: true, homeopathic_general: true,
      }))
      const rows: SpecialtyRecord[] = [
        ...data.specialties.map((s: any, i: number) => ({
          code: s.code, name: s.name,
          can_prescribe: s.can_prescribe ?? true,
          homeopathic: s.homeopathic ?? false,
          homeopathic_general: false,
          rank: typeof s.rank === 'number' ? s.rank : i,
        })),
        ...(data.homeopathic_generalists || []).map((s: any, i: number) => ({
          code: s.code, name: s.name,
          can_prescribe: s.can_prescribe ?? false,
          homeopathic: true, homeopathic_general: true,
          rank: typeof s.rank === 'number' ? s.rank : i,
        })),
      ]
      if (data.specialties.length > 1) {
        setSpecialtyRows(rows)
        sendFilterToParent(filterOptions, params, homeoGeneralists, rows)
      } else {
        setSpecialtyRows([])
      }
    }

    // ONE results handler for both providers and trials. Always lands in
    // phase='results' so the iframe paints from a single code path and
    // the prompt row stays visible regardless of result type.
    const onResults = (data: any) => {
      console.log('[FindCare] stream event kind=results providers=', data?.providers?.length || 0,
                  'trials=', data?.trials?.length || 0)
      if (ac.signal.aborted) return
      if (data.error) {
        finishTimer()
        sendToParent('gui:fatal-error', { message: data.error })
        setError(data.error)
        setPhase('error')
        sawError = true
        return
      }
      setSystemMessage('')
      if (data.providers) {
        sawProviders = true
        const enriched = data.providers.map((p: any) => ({
          ...p,
          specialty: specialtyMapRef.current[p.taxonomy_code] || '',
        }))
        selection.setAvailable(enriched as Provider[])
        if (data.total_count) setTotalCount(data.total_count)
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore((data.providers.length || 0) < (data.total_count || 0))
      }
      if (data.search_params) {
        setSearchParams((prev: any) => ({ ...(prev || {}), ...data.search_params }))
      }
      if (data.trials) {
        const trials = data.trials || []
        setTrialsList(trials)
        setSelectedTrialIndex(0)
        sendToParent('gui:trials-left-panel', { html: buildTrialsLeftPanelHtml(trials) })
      }
      finishTimer()
      setPhase('results')
    }

    try {
      const ssUrl = _sharedServicesUrl(API_URL)
      const body: any = { op: 'utterance', payload: { text } }
      const tok = _sessionToken && _sessionToken.token
      if (tok && typeof tok === 'string' && tok.length >= 32) {
        body.prior_guid = tok.slice(-32)
      }
      const resp = await fetch(`${ssUrl}/gate`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
        },
        body: JSON.stringify(body),
        signal: ac.signal,
      })
      if (!resp.ok || !resp.body) {
        throw new Error(`Gate stream failed: HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed) continue
          let evt: any
          try { evt = JSON.parse(trimmed) } catch { continue }
          if (evt.kind === 'specialties') { onSpecialties(evt.data || {}); sawTerminalEvent = true }
          else if (evt.kind === 'providers' || evt.kind === 'trials') { onResults(evt.data || {}); sawTerminalEvent = true }
          else if (evt.kind === 'prompt') onPrompt(evt.data || {})
          else if (evt.kind === 'final' && !sawError) {
            const okFlag = evt.data?.ok ?? evt.ok
            console.log('[FindCare] stream event kind=final ok=', okFlag,
              'sawPrompt=', sawPrompt, 'sawSpecialties=', sawSpecialties,
              'sawProviders=', sawProviders)
            finishTimer()
            setSearchPromptUp(false)
            if (okFlag === false) {
              const msg = evt.data?.error || evt.error || 'Search failed'
              sendToParent('gui:fatal-error', { message: msg })
              setError(msg)
              setPhase('error')
              sawError = true
            } else if (sawProviders) {
              // Provider/trial results already setPhase('results') via
              // onResults; do not clobber with 'clarify' even if a prompt
              // was streamed earlier this turn.
            } else if (sawPrompt) {
              // Mid-stream prompt was shown; no providers/specialties
              // terminal arrived. Move to 'clarify' so Send re-enables
              // and the user can submit the answer they pre-typed.
              setPhase('clarify')
            } else if (!sawTerminalEvent) {
              setPhase('welcome')
            }
          }
        }
      }
    } catch (err: any) {
      if (err && (err.name === 'AbortError' || ac.signal.aborted)) return
      finishTimer()
      const msg = err.message || 'Search failed'
      sendToParent('gui:fatal-error', { message: msg })
      setError(msg)
      setPhase('error')
    }
  }, [])

  // ── Apply Filter via /gate (EPIC-002-F-010-S-002-REQ-B-002) ────
  // The Apply Filter button in the parent's specialty panel posts
  // back via postMessage. We send op="apply_filter" to SharedServices
  // /gate with the new nucc_codes set. UR reads the carried
  // IntentDocument's geography from session:
  //   - sufficient: UR runs ProviderSearch directly with the new
  //     codes; stream emits kind:"specialties" + kind:"providers" +
  //     kind:"final".
  //   - insufficient: UR dispatches UM as a manufacture-trigger; UM
  //     authors a context-sensitive location prompt; stream emits
  //     kind:"prompt" + kind:"final". Send is re-enabled in
  //     'clarify' phase so the user can supply the missing geography.
  const doApplyFilter = useCallback(async (nuccCodes: string[]) => {
    if (searchAbortRef.current) searchAbortRef.current.abort()
    const ac = new AbortController()
    searchAbortRef.current = ac

    setPhase('searching')
    setThinkSeconds(0)
    setError('')
    setSystemMessage('')
    setSystemCorrections([])
    selection.flushGarbage()
    selection.setAvailable([])

    const start = Date.now()
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setThinkSeconds(Math.round((Date.now() - start) / 1000))
    }, 1000)

    const finishTimer = () => {
      if (timerRef.current) clearInterval(timerRef.current)
      timerRef.current = null
    }

    let sawError = false
    let sawTerminalEvent = false
    let sawPrompt = false
    let sawProviders = false
    let sawSpecialties = false

    const onPrompt = (data: any) => {
      const text = String(data?.text || '').trim()
      if (!text) return
      console.log('[FindCare apply_filter] stream event kind=prompt text=', text)
      setSystemMessage(text)
      setSearchPromptUp(true)
      sawTerminalEvent = true
      sawPrompt = true
    }
    const onSpecialties = (data: any) => {
      console.log('[FindCare apply_filter] stream event kind=specialties count=', data?.specialties?.length || 0)
      sawSpecialties = true
      if (data.error || !data.specialties?.length) return
      const specMap: Record<string, string> = {}
      data.specialties.forEach((s: any) => { specMap[s.code] = s.name })
      specialtyMapRef.current = specMap
      setSearchParams((prev: any) => ({ ...(prev || {}), nucc_codes: data.specialties.map((s: any) => s.code), limit: 25 }))
    }
    // Same single results handler as doSearch — providers + trials both
    // land in phase='results'.
    const onResults = (data: any) => {
      console.log('[FindCare apply_filter] stream event kind=results providers=',
                  data?.providers?.length || 0, 'trials=', data?.trials?.length || 0)
      if (ac.signal.aborted) return
      if (data.error) {
        finishTimer()
        sendToParent('gui:fatal-error', { message: data.error })
        setError(data.error)
        setPhase('error')
        sawError = true
        return
      }
      setSystemMessage('')
      if (data.providers) {
        sawProviders = true
        const enriched = data.providers.map((p: any) => ({
          ...p,
          specialty: specialtyMapRef.current[p.taxonomy_code] || '',
        }))
        selection.setAvailable(enriched as Provider[])
        if (data.total_count) setTotalCount(data.total_count)
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore((data.providers.length || 0) < (data.total_count || 0))
      }
      if (data.search_params) {
        setSearchParams((prev: any) => ({ ...(prev || {}), ...data.search_params }))
      }
      if (data.trials) {
        const trials = data.trials || []
        setTrialsList(trials)
        setSelectedTrialIndex(0)
        sendToParent('gui:trials-left-panel', { html: buildTrialsLeftPanelHtml(trials) })
      }
      finishTimer()
      setPhase('results')
    }

    try {
      const ssUrl = _sharedServicesUrl(API_URL)
      const body: any = { op: 'apply_filter', payload: { nucc_codes: nuccCodes } }
      const tok = _sessionToken && _sessionToken.token
      if (tok && typeof tok === 'string' && tok.length >= 32) {
        body.prior_guid = tok.slice(-32)
      }
      const resp = await fetch(`${ssUrl}/gate`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
        },
        body: JSON.stringify(body),
        signal: ac.signal,
      })
      if (!resp.ok || !resp.body) {
        throw new Error(`Apply Filter /gate stream failed: HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed) continue
          let evt: any
          try { evt = JSON.parse(trimmed) } catch { continue }
          if (evt.kind === 'specialties') { onSpecialties(evt.data || {}); sawTerminalEvent = true }
          else if (evt.kind === 'providers' || evt.kind === 'trials') { onResults(evt.data || {}); sawTerminalEvent = true }
          else if (evt.kind === 'prompt') onPrompt(evt.data || {})
          else if (evt.kind === 'final' && !sawError) {
            const okFlag = evt.data?.ok ?? evt.ok
            console.log('[FindCare apply_filter] stream event kind=final ok=', okFlag,
              'sawPrompt=', sawPrompt, 'sawSpecialties=', sawSpecialties,
              'sawProviders=', sawProviders)
            finishTimer()
            setSearchPromptUp(false)
            if (okFlag === false) {
              const msg = evt.data?.error || evt.error || 'Apply Filter failed'
              sendToParent('gui:fatal-error', { message: msg })
              setError(msg)
              setPhase('error')
              sawError = true
            } else if (sawProviders) {
              // Provider/trial results already setPhase('results') via onResults.
            } else if (sawPrompt) {
              // Manufacture-trigger path: UM authored a prompt. Move
              // to 'clarify' so Send re-enables and the user can
              // supply the missing slot via free-text.
              setPhase('clarify')
            } else if (!sawTerminalEvent) {
              setPhase('welcome')
            }
          }
        }
      }
    } catch (err: any) {
      if (err && (err.name === 'AbortError' || ac.signal.aborted)) return
      finishTimer()
      const msg = err.message || 'Apply Filter failed'
      sendToParent('gui:fatal-error', { message: msg })
      setError(msg)
      setPhase('error')
    }
  }, [])

  // ── Fetch providers (search or filter refresh) ─────────────────
  const fetchProviders = useCallback(async (params: any, q: string) => {
    const ac = searchAbortRef.current
    try {
      const resp = await fetchWithColdStartRetry(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
        signal: ac ? ac.signal : undefined,
      })
      checkSecurityViolation(resp, `${API_URL}/search`)
      // REQ-T-005 fail-hard: a non-200 from /search means the backend
      // assertion fired (e.g. an institution slipped through). Don't paper
      // over it — propagate as a fatal error.
      if (!resp.ok) {
        throw new Error(`Provider search failed: HTTP ${resp.status}`)
      }
      const data = await resp.json()
      // Bug 1: skip state updates if a newer search aborted us mid-flight.
      if (ac && ac.signal.aborted) return
      if (data.providers) {
        // Enrich providers with specialty name from user's selection (FC-DISPLAY-001-REQ-002)
        // Provider's taxonomy_code is guaranteed to be in the selection (queried with $in)
        const enriched = data.providers.map((p: any) => ({
          ...p,
          specialty: specialtyMapRef.current[p.taxonomy_code] || '',
        }))
        selection.setAvailable(enriched as Provider[])
        if (data.total_count) setTotalCount(data.total_count)
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore((data.providers.length || 0) < (data.total_count || 0))
      }
      setPhase('results')
    } catch (err: any) {
      if (err && (err.name === 'AbortError' || (ac && ac.signal.aborted))) return
      const msg = 'Failed to fetch providers'
      sendToParent('gui:fatal-error', { message: msg })
      setPhase('error')
      setError(msg)
    }
  }, [])

  // ── Load more (pagination cursor) ──────────────────────────────
  const loadMore = useCallback(async () => {
    if (!searchParams || !lastNpi || isLoadingMore) return
    setIsLoadingMore(true)
    try {
      const resp = await fetchWithColdStartRetry(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...searchParams, after_npi: lastNpi, limit: 25 }),
      })
      checkSecurityViolation(resp, `${API_URL}/search`)
      const data = await resp.json()
      if (data.providers?.length) {
        // Add to available (reducer will filter out selected/garbage)
        selection.setAvailable([...selection.state.available, ...data.providers])
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore(data.providers.length >= 25)
      } else {
        setHasMore(false)
      }
    } catch {
      // Silent failure on pagination
    }
    setIsLoadingMore(false)
  }, [searchParams, lastNpi, isLoadingMore, selection.state.available])

  // ── Send filter panel to parent ────────────────────────────────
  const sendFilterToParent = useCallback((options: any[], params: any, homeoGeneralists?: any[], rows?: SpecialtyRecord[]) => {
    const prescCount = options.filter((o: any) => o.can_prescribe).length
    const allCount = options.length

    const items = options.map((opt: any) =>
      `<div style="display:block;padding: '1em'.5em 1em;border-bottom:0.125em solid #f0f0f0;" data-spec-code="${opt.code}" data-can-prescribe="${opt.can_prescribe || false}" data-homeopathic="${opt.homeopathic || false}">
        <label style="display:flex;align-items:center;gap:0.75em;cursor:pointer;font-size:1.375em;">
          <input type="checkbox" data-gui-action="filter-toggle" data-gui-value="${opt.code}" checked
            style="accent-color:#0b7a75;width:1.5em;height:1.5em;" />
          <span style="color:#1f2937;">${opt.name}</span>
        </label>
      </div>`
    ).join('')

    // FINDCARE-UX-002: 4-cell reactive-percentage layout using flexbox (reliable
    // % heights, unlike table tr/td). Pixel heights MUST NOT be used. Cells:
    //   cell 1 (header) 18%, cell 2 (specialty scroll, max 12 visible) 40%,
    //   cell 3 (Apply button) 20%, cell 4 (session-verification placeholder) 22%.
    // Cell 2 max-height in em provides the 12-item cap independent of panel
    // height, while flex-basis 40% keeps it reactive. Cell 2 height and scroll
    // position MUST NOT shift when cell 4 populates.
    const html = `
      <div data-filter-panel style="display:flex;flex-direction:column;font-family:system-ui,sans-serif;background:#fff;height:100%;">
          <div data-cell="1" style="flex:0 0 18%;overflow:hidden;">
            <div style="padding:1em 1.25em;border-bottom:0.25em solid #0b7a75;background:#f8fffe;height:100%;box-sizing:border-box;">
              <!-- EPIC-006-F-002-S-001-REQ-B-008: 7 elements in order inside cell 1 (green header):
                   (1) Filter by specialty label, (2) All possible, (3) Prescribers count,
                   (4) Your choices, (5) Uncheck All toggle, (6) Prescribers checkbox,
                   (7) Homeopathic checkbox. Uncheck All sits to the LEFT of the checkbox column. -->
              <div style="display:flex;align-items:center;flex-wrap:wrap;gap:0.5em 0;">
                <div style="flex:0 0 auto;padding-right:1em;border-right:0.125em solid #d8e2e1;">
                  <div style="font-size:1.25em;font-weight:700;color:#0b7a75;text-transform:uppercase;white-space:nowrap;">Filter by specialty</div>
                </div>
                <div style="flex:0 0 auto;padding: '1em' 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size:1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">All possible</div>
                  <div style="font-size:1.625em;font-weight:700;color:#1f2937;">${allCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: '1em' 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size:1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Prescribers</div>
                  <div style="font-size:1.625em;font-weight:700;color:#1f2937;" id="filterFilteredCount">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: '1em' 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size:1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Your choices</div>
                  <div style="font-size:1.625em;font-weight:700;color:#0b7a75;" id="filterShowing">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: '1em' 1em;border-right:0.125em solid #d8e2e1;display:flex;align-items:center;">
                  <button data-gui-action="toggle-all"
                    style="background:#fff;border:0.125em solid #0b7a75;border-radius:0.375em;padding: '1em'.375em 1.25em;font-size:1.25em;color:#0b7a75;cursor:pointer;font-weight:600;white-space:nowrap;">Uncheck All</button>
                </div>
                <div style="flex:0 0 auto;padding-left:1em;display:flex;flex-direction:column;gap:0.375em;">
                  <label style="font-size:1.25em;color:#1f2937;display:flex;align-items:center;gap:0.5em;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="prescribers" checked
                      style="accent-color:#0b7a75;width:1.625em;height:1.625em;" /> Prescribers
                  </label>
                  <label style="font-size:1.25em;color:#1f2937;display:flex;align-items:center;gap:0.5em;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="homeopathic"
                      style="accent-color:#0b7a75;width:1.625em;height:1.625em;" /> Homeopathic
                  </label>
                </div>
              </div>
            </div>
          </div>
          <div data-cell="2" style="flex:0 0 40%;max-height:22em;overflow-y:auto;overflow-x:hidden;">${items}</div>
          <div data-cell="3" style="flex:0 0 20%;padding: '1em'.75em 1em;border-top:0.125em solid #d8e2e1;box-sizing:border-box;overflow:hidden;">
            <button data-gui-action="filter-apply" style="width:100%;padding: '1em'.625em;border-radius:0.5em;border:none;background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;font-size:1.375em;font-weight:600;cursor:pointer;">Apply Filter</button>
          </div>
          <div data-cell="4" id="guiSessionCell" style="flex:0 0 22%;padding: '1em'.5em 1em;border-top:0.125em solid #e5e7eb;box-sizing:border-box;overflow:hidden;"></div>
      </div>`

    sendToParent('gui:filter', {
      html,
      searchParams: JSON.stringify(params),
      applyInitialFilter: true,
      homeopathicGeneralists: homeoGeneralists || [],
      // Option B: structured rows for the parent to forward into the
      // filter sub-iframe (which renders SpecialtyFilter against them).
      specialties: rows ?? options.map((o, i) => ({
        code: o.code, name: o.name,
        can_prescribe: !!o.can_prescribe, homeopathic: !!o.homeopathic,
        homeopathic_general: false, rank: i,
      })),
    })
  }, [])

  // ── Evaluate handoff ───────────────────────────────────────────
  const handleEvaluate = useCallback(async () => {
    const providers = selectedRef.current
    if (providers.length === 0) {
      alert('Select at least one provider before evaluating.')
      return
    }

    const token = await getSessionToken()

    try {
      const resp = await fetch(`${EVALCARE_URL}/evaluate/providers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          providers,
          session_token: token,
          question_summary: question,
        }),
      })
      const data = await resp.json()

      sendToParent('gui:evaluate-result', {
        providers,
        question,
        session_token: data.session_token || null,
      })
    } catch (err: any) {
      // ONE failure path: the canonical chFatalError overlay. Carry full
      // diagnostic context so the operator gets a real error report, not
      // a wimpy popup.
      const lines = [
        `[EvaluateCare /evaluate/providers] fetch failed`,
        ``,
        `When:      ${new Date().toISOString()}`,
        `Iframe:    ${window.location.origin}${window.location.pathname}`,
        `Method:    POST`,
        `URL:       ${EVALCARE_URL}/evaluate/providers`,
        `Providers: ${providers.length} selected`,
        `Question:  ${question ? JSON.stringify(question).slice(0, 200) : '(empty)'}`,
        `Token:     ${token ? 'present' : 'MISSING — verify-token may have failed upstream'}`,
        `User-Agent: ${navigator.userAgent}`,
        ``,
        `Error:`,
        `  name:    ${err?.name || 'Error'}`,
        `  message: ${err?.message || '(no message)'}`,
        `  cause:   ${err?.cause ? String(err.cause) : '(no cause)'}`,
        ``,
        `Likely causes for "Failed to fetch" at this layer:`,
        `  1. Browser has not accepted the self-signed cert at ${EVALCARE_URL} — open that URL directly in a tab and accept the warning.`,
        `  2. Cross-origin preflight (OPTIONS) rejected — check evalcare's CORSMiddleware allow_origins covers ${window.location.origin}.`,
        `  3. ${EVALCARE_URL.replace(/^https?:\/\//, '')} container is down or not bound to that port.`,
        `  4. Mixed-content block (http upstream from https iframe).`,
        ``,
        `Stack:`,
        err?.stack || '  (no stack)',
      ]
      sendToParent('gui:fatal-error', { message: lines.join('\n') })
    }
  }, [question])

  // ── Provider detail click — click path, NOT an utterance ─────────
  // POSTs to SharedServices /gate with op="provider-detail" and the
  // card fields. The gateway routes the click to provider_detail_tool
  // (no LLM hop, no utterance manager). The response is forwarded to
  // the parent wrapper which paints the right panel.
  const openProviderDetail = useCallback(async (p: Provider) => {
    const ssUrl = _sharedServicesUrl(API_URL)
    const tok = _sessionToken && _sessionToken.token
    const body: any = {
      op: 'provider-detail',
      payload: {
        name: p.name,
        npi: p.npi,
        specialty: p.specialty || p.primary_specialty || null,
        address: p.address || null,
        county: p.county || null,
        phone: p.phone || null,
        state: p.state || null,
      },
    }
    if (tok && typeof tok === 'string' && tok.length >= 32) {
      body.prior_guid = tok.slice(-32)
    }
    try {
      const resp = await fetch(`${ssUrl}/gate`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
        },
        body: JSON.stringify(body),
      })
      if (!resp.ok || !resp.body) {
        throw new Error(`Gate stream failed: HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let detail: any = null
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed) continue
          let evt: any
          try { evt = JSON.parse(trimmed) } catch { continue }
          if (evt.kind === 'provider-detail') detail = evt.data
        }
      }
      if (detail) {
        sendToParent('gui:provider-detail', { detail, provider: p })
      }
    } catch (err: any) {
      sendToParent('gui:fatal-error', {
        message: `[provider-detail] ${err?.message || 'fetch failed'} for NPI ${p.npi}`,
      })
    }
  }, [])

  // ── Handle send ────────────────────────────────────────────────
  // Single point: the Send button calls doSearch which opens ONE /gate
  // NDJSON stream. The universal_navigation_tool routes "utterance",
  // captures the text into session_conversation_history.utterances on
  // its own, runs specialty_filter + provider_search server-to-server,
  // and emits events. No fire-and-forget side calls from the client.
  const handleSend = (e?: React.FormEvent) => {
    e?.preventDefault()
    const text = input.trim()
    if (!text) return
    setInput('')
    doSearch(text)
  }

  if (!tokenReady) {
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        height: '100vh', fontFamily: 'system-ui, sans-serif', color: '#0b7a75', gap: '1em',
      }}>
        <div style={{ fontSize: '1em', fontWeight: 700 }}>Loading</div>
        <div style={{ fontSize: '1em' }}>{loadingSeconds}s</div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>

      {/* WELCOME PHASE */}
      {phase === 'welcome' && (
        <div style={{ flex: 1, overflowY: 'auto', padding: '1em', maxWidth: 800, margin: '1em', width: '100%' }}>
          <div style={{
            padding: '1em', borderRadius: '2.25em 2.25em 2.25em 0.5em', background: '#fff',
            border: '0.125em solid #e5e7eb', fontSize: '1em', lineHeight: 1.6,
          }} dangerouslySetInnerHTML={{ __html: welcomeHtml }} />
        </div>
      )}

      {/* CLARIFY PHASE — generic clarification bubble from the server.
          Trial detail is rendered by the unified 'results' block. */}
      {phase === 'clarify' && (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          {question && renderQuestionBanner(question, systemCorrections)}
          <div style={{ flex: 1, overflowY: 'auto', padding: '1em', maxWidth: 1100, margin: '1em', width: '100%' }}>
            <div style={{
              padding: '1em', borderRadius: '2.25em 2.25em 2.25em 0.5em', background: '#fff',
              border: '0.125em solid #e5e7eb', fontSize: '1em', lineHeight: 1.6, color: '#0b7a75',
            }}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  table: ({node, ...p}) => <table {...p} style={{borderCollapse: 'collapse', width: '100%', margin: '0.5em 0'}} />,
                  th: ({node, ...p}) => <th {...p} style={{border: '0.0625em solid #d1d5db', padding: '0.5em', background: '#f3f4f6', textAlign: 'left'}} />,
                  td: ({node, ...p}) => <td {...p} style={{border: '0.0625em solid #d1d5db', padding: '0.5em', verticalAlign: 'top'}} />,
                  a: ({node, ...p}) => <a {...p} target="_blank" rel="noopener noreferrer" />,
                }}
              >{systemMessage}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}

      {/* SEARCHING PHASE */}
      {phase === 'searching' && (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, padding: '1em' }}>
          {searchPromptUp && systemMessage ? (
            <div style={{
              padding: '1em', borderRadius: '2.25em 2.25em 2.25em 0.5em', background: '#fff',
              border: '0.125em solid #e5e7eb', fontSize: '1em', lineHeight: 1.6, color: '#0b7a75', maxWidth: 800,
            }}>{renderSystemMessageWithCorrections(systemMessage, systemCorrections)}</div>
          ) : (
            <div style={{ fontSize: '1em', color: '#6b7280' }}>Searching for: <strong>{question}</strong></div>
          )}
          <div style={{ fontSize: '1em', color: '#0b7a75', fontWeight: 700 }}>{thinkSeconds}s</div>
          <div style={{ fontSize: '1em', color: '#9ca3af' }}>
            {searchPromptUp ? 'Server still working — Send unlocks when the response completes.' : 'Waiting for response...'}
          </div>
        </div>
      )}

      {/* ERROR PHASE */}
      {phase === 'error' && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1em' }}>
          <div style={{ padding: '1em', background: '#fef2f2', border: '0.125em solid #fecaca', borderRadius: 8, color: '#dc2626', maxWidth: 500 }}>
            <strong>Error:</strong> {error}
          </div>
        </div>
      )}

      {/* RESULTS PHASE — single paint path for both providers AND
          clinical trials. Trial detail and provider list both live here. */}
      {phase === 'results' && trialsList.length > 0 && (
        <>
          {renderQuestionBanner(
            question,
            systemCorrections,
            `${trialsList.length} clinical trials — viewing ${trialsList[selectedTrialIndex]?.nct_id || trialsList[0]?.nct_id || ''}`,
          )}
          <div style={{ flex: 1, overflowY: 'auto', padding: '1em', maxWidth: 1100, margin: '1em', width: '100%' }}>
            <div style={{
              padding: '1em', borderRadius: '2.25em 2.25em 2.25em 0.5em', background: '#fff',
              border: '0.125em solid #e5e7eb', fontSize: '1em', lineHeight: 1.6, color: '#0b7a75',
            }}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  table: ({node, ...p}) => <table {...p} style={{borderCollapse: 'collapse', width: '100%', margin: '0.5em 0'}} />,
                  th: ({node, ...p}) => <th {...p} style={{border: '0.0625em solid #d1d5db', padding: '0.5em', background: '#f3f4f6', textAlign: 'left'}} />,
                  td: ({node, ...p}) => <td {...p} style={{border: '0.0625em solid #d1d5db', padding: '0.5em', verticalAlign: 'top'}} />,
                  a: ({node, ...p}) => <a {...p} target="_blank" rel="noopener noreferrer" />,
                }}
              >{formatTrialDetail(trialsList[selectedTrialIndex] || trialsList[0])}</ReactMarkdown>
            </div>
          </div>
        </>
      )}
      {phase === 'results' && trialsList.length === 0 && (
        <>
          {/* Question bar */}
          {renderQuestionBanner(question, systemCorrections, `${totalCount} providers found`)}

          {/* SpecialtyFilter is now hosted in the parent's leftPanel
              (legacy HTML render of the 4-cell grid). React component
              version disabled inside the iframe until placement is
              resolved at the architecture layer. */}

          {/* Available providers — scrollable top half */}
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }} data-testid="available-providers">
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '1em', background: '#fafafa', borderBottom: '0.125em solid #eee',
            }}>
              <span style={{ fontSize: '1em', fontWeight: 600, color: '#0b7a75', textTransform: 'uppercase' }}>
                Available Providers
              </span>
              <span style={{ fontSize: '1em', color: '#6b7280' }}>
                {selection.state.available.length} available
                {selection.state.garbage.length > 0 && (
                  <span style={{ color: '#dc2626', marginLeft: '1em' }}>🗑 {selection.state.garbage.length}</span>
                )}
              </span>
            </div>

            {/* EPIC-006-F-002-S-001-REQ-B-001: during Apply-Filter re-classify,
                the unpicked-providers list is turned 'white' and the main-screen
                timer is shown in its place. */}
            {reclassifying && (
              <div
                data-testid="reclassify-timer"
                style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '1em', gap: 12 }}
              >
                <div style={{ fontSize: '1em', color: '#6b7280' }}>Re-querying providers…</div>
                <div style={{ fontSize: '1em', color: '#0b7a75', fontWeight: 700 }}>{thinkSeconds}s</div>
                <div style={{ fontSize: '1em', color: '#9ca3af' }}>Waiting for response...</div>
              </div>
            )}

            {!reclassifying && selection.state.available.map((p: Provider) => (
              <ProviderCard
                key={p.npi}
                provider={p}
                mode="available"
                onSelect={selection.select}
                onDismiss={selection.dismiss}
                onDetail={openProviderDetail}
                selectionFull={selection.isFull}
              />
            ))}

            {!reclassifying && hasMore && (
              <div style={{ padding: '1em', textAlign: 'center' }}>
                <button
                  onClick={loadMore}
                  disabled={isLoadingMore}
                  style={{
                    padding: '1em', borderRadius: 4, border: '0.125em solid #0b7a75',
                    background: '#f0fffe', color: '#0b7a75', fontSize: '1em', fontWeight: 600,
                    cursor: isLoadingMore ? 'wait' : 'pointer',
                  }}
                >
                  {isLoadingMore ? 'Loading...' : `Load more (${totalCount - selection.state.available.length - selection.state.selected.length} remaining)`}
                </button>
              </div>
            )}
          </div>

          {/* Selected providers — sticky bottom half (EPIC-006-F-001-S-002-REQ-B-002: drop target) */}
          <div
            style={{
              borderTop: '0.25em solid #d97706', background: '#fffdf7',
              minHeight: 60, maxHeight: '35%', overflowY: 'auto', flexShrink: 0,
            }}
            onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
            onDrop={(e) => { e.preventDefault(); const npi = e.dataTransfer.getData('text/plain'); if (npi) selection.select(npi) }}
          >
            <div style={{
              padding: '0.5em 1em', background: '#fffbeb',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1em',
            }}>
              <span style={{ fontSize: '1em', fontWeight: 600, color: '#d97706', textTransform: 'uppercase' }}>
                Selected for Evaluation
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: '1em' }}>
                <span style={{ fontSize: '1em', color: '#6b7280' }}>
                  {selection.state.selected.length} / {selection.state.maxSelected}
                </span>
                {selection.state.selected.length > 0 && (
                  <button
                    data-testid="evaluate-button"
                    onClick={() => {
                      sendToParent('gui:start-evaluate')
                      handleEvaluate()
                    }}
                    style={{
                      padding: '0.4em 1em', borderRadius: '0.5em', border: 'none',
                      background: 'linear-gradient(180deg,#d97706,#b45309)', color: '#fff',
                      fontSize: '1em', fontWeight: 700, cursor: 'pointer',
                    }}
                  >
                    Evaluate {selection.state.selected.length} Provider{selection.state.selected.length > 1 ? 's' : ''}
                  </button>
                )}
              </div>
            </div>

            {selection.state.selected.length === 0 ? (
              <div style={{ padding: '0.5em 1em', textAlign: 'center', color: '#9ca3af', fontSize: '1em' }}>
                Click ↓ to select providers (max {selection.state.maxSelected})
              </div>
            ) : (
              selection.state.selected.map((p: Provider) => (
                <ProviderCard
                  key={p.npi}
                  provider={p}
                  mode="selected"
                  compact={true}
                  onDeselect={selection.deselect}
                  draggable={false}
                />
              ))
            )}
          </div>
        </>
      )}

      {/* Input bar — always visible. Send button doubles as Stop while a
          search or filter-reclassify is in flight (click aborts the
          pending fetch). The redundant prompt-row timer was removed
          along with the wrapper-side control frame. */}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (phase === 'searching' || reclassifying) {
            // Stream still open. If the server has emitted a mid-stream
            // prompt (searchPromptUp), the input is unlocked so the user
            // can pre-type the answer — but Send is gated until the
            // stream actually closes. No-op the submit so the answer
            // stays in the input; we'll fire it for real on Send after
            // phase moves to 'clarify'.
            if (searchPromptUp) return
            // No prompt up — Stop button behavior: abort the in-flight
            // fetch.
            if (searchAbortRef.current) searchAbortRef.current.abort()
            if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
            setReclassifying(false)
            if (phase === 'searching') setPhase(question ? 'results' : 'welcome')
            return
          }
          handleSend()
        }}
        style={{
          padding: '0.67em 1em', borderTop: '0.125em solid #e5e7eb',
          display: 'flex', gap: 8, alignItems: 'center', background: '#fff',
        }}
      >
        {(phase === 'searching' || reclassifying) && (
          <div
            data-testid="prompt-row-timer"
            style={{
              flex: '0 0 auto', minWidth: 44, padding: '0.67em 1em', borderRadius: 6,
              background: '#f0fffe', border: '0.125em solid #0b7a75',
              fontSize: '1em', fontWeight: 700, color: '#0b7a75', textAlign: 'center',
            }}
          >{thinkSeconds}s</div>
        )}
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Type a message..."
          // Input is unlocked when a mid-stream prompt arrives, so the
          // user can compose the answer while the server finishes.
          disabled={(phase === 'searching' && !searchPromptUp) || reclassifying}
          style={{
            flex: 1, padding: '0.67em 1em', borderRadius: 8,
            border: '0.125em solid #d1d5db', fontSize: '1em', outline: 'none',
            minHeight: 44,
          }}
        />
        <button
          type="submit"
          style={{
            padding: '0.67em 1em', borderRadius: 8, border: 'none',
            background: searchPromptUp
              ? '#9ca3af'  // gray: waiting for stream close
              : (phase === 'searching' || reclassifying) ? '#b91c1c' : '#0b7a75',
            color: '#fff', fontSize: '1em', fontWeight: 600,
            cursor: searchPromptUp ? 'not-allowed' : 'pointer',
            minHeight: 44, minWidth: 44,
          }}
        >{searchPromptUp ? 'Wait…' : (phase === 'searching' || reclassifying) ? 'Stop' : 'Send'}</button>
      </form>
    </div>
  )
}
