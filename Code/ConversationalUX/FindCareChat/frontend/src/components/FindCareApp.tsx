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
const SHAREDSERVICES_URL = import.meta.env.VITE_SHAREDSERVICES_URL ?? ''

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
  // Preferred path: build-time-injected URL (VITE_SHAREDSERVICES_URL).
  // Works uniformly across local / dev / qa / prod because HF Space names
  // carry per-env numeric suffixes that defeat substring-based rewriting.
  if (SHAREDSERVICES_URL) return SHAREDSERVICES_URL
  // Fallback: derive from the iframe's origin. Kept for back-compat with
  // builds that predate VITE_SHAREDSERVICES_URL injection.
  const base = apiUrl || (typeof window !== 'undefined' ? window.location.origin : '')
  if (base.includes('find-care-chat')) {
    return base.replace('find-care-chat', 'shared-services')
  }
  if (base.includes('chathealthyspace.hf.space')) {
    return base.replace('chathealthyspace.hf.space', 'sharedservicesspace.hf.space')
  }
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


// Trial presentation moved to Website/index.html as ClinicalTrialRenderer.
// The tool returns structured data; the parent owns rendering. Nothing
// here transforms trial data.

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
function _firstSentence(text: string): string {
  const s = String(text || '').trim()
  if (!s) return ''
  const match = s.match(/^[\s\S]*?[.!?](?=\s|$)/)
  if (!match) return s
  const firstSentence = match[0]
  return firstSentence.length < s.length ? firstSentence + '...' : firstSentence
}
function buildTrialsLeftPanelHtml(
  trials: any[],
  totalCount?: number | null,
  pageSize?: number | null,
  hasPrev?: boolean,
  hasMore?: boolean,
  pageStart?: number,
  searchContext?: any,
): string {
  const items = trials.map((t: any, i: number) => {
    const nct = _escapeHtml(t.nct_id || '(no id)')
    const title = _escapeHtml(t.brief_title || '')
    const summary = _escapeHtml(_firstSentence(t.brief_summary || ''))
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
        ${summary ? `<div style="font-size:0.85em; color:#4b5563; margin:0.15em 0 0.35em 0; font-style:italic;">${summary}</div>` : ''}
        <ul style="margin:0.25em 0 0 1em; padding:0; font-size:0.9em; color:#4b5563;">
          <li><strong>Conditions:</strong> ${conds}</li>
          <li><strong>Distance to nearest site:</strong> ${dist}</li>
          <li><strong>Age range:</strong> ${ages}</li>
          <li><strong>Sex:</strong> ${sex}</li>
        </ul>
      </li>`
  }).join('')

  const total = (typeof totalCount === 'number' && totalCount > 0) ? totalCount : null
  const start = Math.max(1, pageStart || 1)
  const end = start + trials.length - 1
  // Header band shows what was actually queried (echoed by the tool in
  // search_context). When we know the real total, append "showing X to Y
  // of Z"; otherwise just the query terms (we never lie about the count).
  const qc: any = searchContext || {}
  const queryParts: string[] = []
  if (qc.condition) queryParts.push(`condition: ${qc.condition}`)
  if (qc.age_years != null) queryParts.push(`subject age: ${qc.age_years}`)
  if (qc.sex) queryParts.push(`subject sex: ${qc.sex}`)
  if (qc.gender) queryParts.push(`subject gender: ${qc.gender}`)
  const queryStr = queryParts.join(', ')
  const countStr = total ? ` — showing ${start} to ${end} of ${total}` : ''
  const headerText = queryStr
    ? `Clinical trials — ${queryStr}${countStr}`
    : `Clinical trials${countStr}`
  const moreLink = hasMore
    ? `<a href="#" data-trial-page="next" style="color:#0b7a75; text-decoration:underline; cursor:pointer; font-weight:700;">more trials</a>`
    : ''
  const backLink = hasPrev
    ? `<a href="#" data-trial-page="prev" style="color:#0b7a75; text-decoration:underline; cursor:pointer; font-weight:700;">back</a>`
    : ''
  const ctrlSep = (moreLink && backLink) ? ' &nbsp;|&nbsp; ' : ''
  const ctrlRow = (moreLink || backLink)
    ? `<div style="margin-top:0.75em; font-size:0.95em; color:#374151;">
         ${backLink}${ctrlSep}${moreLink}
       </div>`
    : ''

  return `
    <div style="height:100%; overflow-y:auto; padding:1em; box-sizing:border-box;">
      <div style="font-size:1em; font-weight:700; color:#0b7a75; text-transform:uppercase; margin-bottom:0.5em;">
        ${_escapeHtml(headerText)}
      </div>
      <ul style="list-style:disc; padding-left:1.25em; margin:0;">${items}</ul>
      ${ctrlRow}
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
  const [trialsTotalCount, setTrialsTotalCount] = useState<number | null>(null)
  const [trialsPageSize, setTrialsPageSize] = useState<number>(10)
  const [trialsPageStart, setTrialsPageStart] = useState<number>(1)
  const trialsNextCursorRef = useRef<string | null>(null)
  const trialsPrevCursorsRef = useRef<string[]>([])
  const trialsLastQueryRef = useRef<{
    condition: string;
    user_location: string | null;
    age_years: number | null;
    sex: string | null;
    gender: string | null;
  } | null>(null)
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

      if (msg.type === 'trial:page' && (msg.direction === 'next' || msg.direction === 'prev')) {
        const last = trialsLastQueryRef.current
        if (!last) return
        let cursor: string | null = null
        let newPageStart = trialsPageStart
        if (msg.direction === 'next') {
          cursor = trialsNextCursorRef.current
          if (!cursor) return
          trialsPrevCursorsRef.current.push(cursor)
          newPageStart = trialsPageStart + trialsList.length
        } else {
          trialsPrevCursorsRef.current.pop()
          cursor = trialsPrevCursorsRef.current.length
            ? trialsPrevCursorsRef.current[trialsPrevCursorsRef.current.length - 1]
            : null
          newPageStart = Math.max(1, trialsPageStart - trialsPageSize)
        }

        // Immediate UX feedback before the network round-trip:
        //   - center detail panel cleared (phase=searching renders the
        //     timer/loading screen instead of the trial detail)
        //   - right panel cleared (wrapper listens for trials:clear)
        //   - left panel header shows the loading state
        //   - timer starts ticking from 0
        // The searching banner shows the CANONICAL criteria (condition +
        // demographics) plus the record range being retrieved so the user
        // sees exactly what the system is doing — not just a raw verb.
        const _critParts: string[] = []
        if (last.condition) _critParts.push(last.condition)
        if (last.age_years != null) _critParts.push(`age ${last.age_years}`)
        if (last.sex) _critParts.push(`sex ${last.sex}`)
        if (last.gender) _critParts.push(`gender ${last.gender}`)
        if (last.user_location) _critParts.push(last.user_location)
        const _crit = _critParts.join(', ')
        const _endRange = newPageStart + trialsPageSize - 1
        const _ofTotal = trialsTotalCount ? ` of ${trialsTotalCount}` : ''
        setQuestion(`${_crit} — retrieving records ${newPageStart}–${_endRange}${_ofTotal}`)
        setPhase('searching')
        setThinkSeconds(0)
        setTrialsList([])
        setSelectedTrialIndex(0)
        sendToParent('gui:trials-clear', {})
        const _pageStart = Date.now()
        if (timerRef.current) clearInterval(timerRef.current)
        timerRef.current = setInterval(() => {
          setThinkSeconds(Math.round((Date.now() - _pageStart) / 1000))
        }, 1000)
        const _finishPageTimer = () => {
          if (timerRef.current) clearInterval(timerRef.current)
          timerRef.current = null
        }

        const gateBody: any = {
          op: 'clinical_trials_page',
          payload: {
            condition: last.condition,
            user_location: last.user_location || null,
            page_size: trialsPageSize,
            cursor,
            age_years: last.age_years,
            sex: last.sex,
            gender: last.gender,
          },
        }
        const tokForPage = _sessionToken && _sessionToken.token
        if (tokForPage && typeof tokForPage === 'string' && tokForPage.length >= 32) {
          gateBody.prior_guid = tokForPage.slice(-32)
        }
        const ssUrlForPage = _sharedServicesUrl(API_URL)
        fetch(`${ssUrlForPage}/gate`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/x-ndjson',
          },
          body: JSON.stringify(gateBody),
        })
          .then(async resp => {
            if (!resp.ok || !resp.body) {
              throw new Error(`Gate stream failed: HTTP ${resp.status}`)
            }
            const reader = resp.body.getReader()
            const decoder = new TextDecoder()
            let buffer = ''
            let trialsData: any = null
            while (true) {
              const { done, value } = await reader.read()
              if (done) break
              buffer += decoder.decode(value, { stream: true })
              let nl
              while ((nl = buffer.indexOf('\n')) >= 0) {
                const line = buffer.substring(0, nl).trim()
                buffer = buffer.substring(nl + 1)
                if (!line) continue
                try {
                  const evt = JSON.parse(line)
                  if (evt && evt.kind === 'trials') {
                    trialsData = evt.data || {}
                  }
                } catch { /* ignore non-JSON */ }
              }
            }
            if (!trialsData) throw new Error('no trials event in stream')
            const trials = trialsData.trials || []
            setTrialsList(trials)
            setSelectedTrialIndex(0)
            trialsNextCursorRef.current = trialsData.cursor || null
            if (typeof trialsData.total_count === 'number') setTrialsTotalCount(trialsData.total_count)
            setTrialsPageStart(newPageStart)
            const ctx2 = trialsData.search_context || trialsLastQueryRef.current || {}
            const hasPrev = trialsPrevCursorsRef.current.length > 0
            const hasMore = !!trialsData.cursor
            sendToParent('gui:trials-left-panel', {
              html: buildTrialsLeftPanelHtml(
                trials,
                trialsData.total_count ?? trialsTotalCount,
                trialsData.page_size ?? trialsPageSize,
                hasPrev,
                hasMore,
                newPageStart,
                ctx2,
              ),
            })
            _finishPageTimer()
            setPhase('results')
          })
          .catch(err => {
            _finishPageTimer()
            setPhase('results')
            const m = err && err.message ? err.message : 'trials page /gate stream failed'
            const verb = msg.direction === 'next' ? 'load more clinical trials' : 'go back to the previous page of clinical trials'
            sendToParent('gui:fatal-error', {
              message: `While trying to ${verb}: ${m}`,
            })
          })
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

      // Phase B — parent owns the input form. Parent posts the typed text
      // here; we run it through the same handleSend pipeline that the
      // in-React form used before.
      if (msg.type === 'user_input' && typeof msg.text === 'string') {
        setInput(msg.text)
        doSearch(msg.text)
      }

      // Phase B — parent's Stop button cancels in-flight searches.
      if (msg.type === 'user_stop') {
        if (searchAbortRef.current) searchAbortRef.current.abort()
        if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
        setReclassifying(false)
        if (phase === 'searching') setPhase(question ? 'results' : 'welcome')
      }

      // Phase B — parent forwards selection-list interactions.
      if (msg.type === 'provider_select' && typeof msg.npi === 'string') {
        selection.select(msg.npi)
      }
      if (msg.type === 'provider_dismiss' && typeof msg.npi === 'string') {
        selection.dismiss(msg.npi)
      }
      if (msg.type === 'provider_unselect' && typeof msg.npi === 'string') {
        selection.deselect(msg.npi)
      }
      if (msg.type === 'load_more_providers') {
        loadMore()
      }
    }
    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [phase, question, hasMore, isLoadingMore])

  // Phase B — Parent owns frame painting. Broadcast renderable state on
  // every change; index.html's gui:findcare-state handler paints centerPanel
  // and the input form based on what arrives. React renders no DOM.
  useEffect(() => {
    sendToParent('gui:findcare-state', {
      ready: tokenReady,
      loadingSeconds,
      phase,
      welcomeHtml,
      question,
      systemMessage,
      systemCorrections,
      thinkSeconds,
      error,
      // Architecture: the FindClinicalTrials tool returns structured trial
      // objects. React broadcasts the raw object — the parent owns trial
      // presentation. No markdown rendering or HTML formatting here.
      trial: trialsList.length
        ? (trialsList[selectedTrialIndex] || trialsList[0])
        : null,
      trialCount: trialsList.length,
      trialNctId: trialsList[selectedTrialIndex]?.nct_id || trialsList[0]?.nct_id || '',
      available: selection.state.available.map((p: any) => ({
        npi: p.npi,
        name: p.name,
        specialty: p.specialty || p.primary_specialty || '',
        address: p.address,
        city: p.city,
        state: p.state,
        zip: p.zip,
        county: p.county,
        phone: p.phone,
      })),
      selected: selection.state.selected.map((p: any) => ({
        npi: p.npi,
        name: p.name,
        specialty: p.specialty || p.primary_specialty || '',
        address: p.address,
        city: p.city,
        state: p.state,
        zip: p.zip,
        county: p.county,
        phone: p.phone,
      })),
      garbageCount: selection.state.garbage.length,
      totalCount,
      hasMore,
      isLoadingMore,
      searchPromptUp,
      reclassifying,
    })
  }, [tokenReady, loadingSeconds, phase, welcomeHtml, question, systemMessage,
      systemCorrections, thinkSeconds, error, trialsList, selectedTrialIndex,
      selection.state.available, selection.state.selected, selection.state.garbage.length,
      totalCount, hasMore, isLoadingMore, searchPromptUp, reclassifying])

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
    let sawTrials = false
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
        sawTrials = true
        const trials = data.trials || []
        setTrialsList(trials)
        setSelectedTrialIndex(0)
        const tc = typeof data.total_count === 'number' ? data.total_count : null
        const ps = typeof data.page_size === 'number' ? data.page_size : 10
        setTrialsTotalCount(tc)
        setTrialsPageSize(ps)
        setTrialsPageStart(1)
        trialsNextCursorRef.current = data.cursor || null
        trialsPrevCursorsRef.current = []
        // Authoritative search context echoed by the tool — use this to
        // re-issue pagination so the raw user utterance never leaks into
        // the CT.gov call.
        const ctx = data.search_context || {}
        trialsLastQueryRef.current = {
          condition: ctx.condition || '',
          user_location: ctx.user_location || null,
          age_years: typeof ctx.age_years === 'number' ? ctx.age_years : null,
          sex: ctx.sex || null,
          gender: ctx.gender || null,
        }
        const hasMore = !!data.cursor
        sendToParent('gui:trials-left-panel', {
          html: buildTrialsLeftPanelHtml(trials, tc, ps, false, hasMore, 1, ctx),
        })
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
          else if (evt.kind === 'intent_classified') {
            const d = evt.data || {}
            if (d.action === 'findClinicalTrials') {
              const _p: string[] = []
              if (d.condition) _p.push(d.condition)
              if (d.age_years != null) _p.push(`age ${d.age_years}`)
              if (d.sex) _p.push(`sex ${d.sex}`)
              if (d.gender) _p.push(`gender ${d.gender}`)
              if (d.user_location) _p.push(d.user_location)
              if (_p.length) setQuestion(_p.join(', '))
            }
          }
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
            } else if (sawProviders || sawTrials) {
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
      sendToParent('gui:fatal-error', { message: msg, httpStatus: err && err.httpStatus })
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
    let sawTrials = false
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
        sawTrials = true
        const trials = data.trials || []
        setTrialsList(trials)
        setSelectedTrialIndex(0)
        const tc = typeof data.total_count === 'number' ? data.total_count : null
        const ps = typeof data.page_size === 'number' ? data.page_size : 10
        setTrialsTotalCount(tc)
        setTrialsPageSize(ps)
        setTrialsPageStart(1)
        trialsNextCursorRef.current = data.cursor || null
        trialsPrevCursorsRef.current = []
        // Authoritative search context echoed by the tool — use this to
        // re-issue pagination so the raw user utterance never leaks into
        // the CT.gov call.
        const ctx = data.search_context || {}
        trialsLastQueryRef.current = {
          condition: ctx.condition || '',
          user_location: ctx.user_location || null,
          age_years: typeof ctx.age_years === 'number' ? ctx.age_years : null,
          sex: ctx.sex || null,
          gender: ctx.gender || null,
        }
        const hasMore = !!data.cursor
        sendToParent('gui:trials-left-panel', {
          html: buildTrialsLeftPanelHtml(trials, tc, ps, false, hasMore, 1, ctx),
        })
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
          else if (evt.kind === 'intent_classified') {
            const d = evt.data || {}
            if (d.action === 'findClinicalTrials') {
              const _p: string[] = []
              if (d.condition) _p.push(d.condition)
              if (d.age_years != null) _p.push(`age ${d.age_years}`)
              if (d.sex) _p.push(`sex ${d.sex}`)
              if (d.gender) _p.push(`gender ${d.gender}`)
              if (d.user_location) _p.push(d.user_location)
              if (_p.length) setQuestion(_p.join(', '))
            }
          }
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
            } else if (sawProviders || sawTrials) {
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
      sendToParent('gui:fatal-error', { message: msg, httpStatus: err && err.httpStatus })
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
      sendToParent('gui:fatal-error', { message: msg, httpStatus: err && err.httpStatus })
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
      sendToParent('gui:fatal-error', { message: lines.join('\n'), httpStatus: err && err.httpStatus })
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
        httpStatus: err && err.httpStatus,
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

  // Phase B — parent owns all rendering. React broadcasts state via
  // useEffect above; this component returns no DOM.
  return null
}
