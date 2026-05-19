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
import { useSelectionState } from '@providers/useSelectionState'
import { ProviderCard } from '@providers/ProviderCard'
import type { Provider } from '@providers/provider'
import SpecialtyFilter, { type SpecialtyRecord } from './SpecialtyFilter'

const API_URL = import.meta.env.VITE_API_URL ?? ''
const EVALCARE_URL = import.meta.env.VITE_EVALCARE_URL ?? ''

type Phase = 'welcome' | 'searching' | 'results' | 'error'

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

// ── SharedServices URL resolver ──────────────────────────────────
// The iframe's API_URL points at the FindCare backend; SharedServices
// runs on a parallel host. Derive the SS URL from the host pattern so
// we don't hardcode env-specific routing here.
function _sharedServicesUrl(apiUrl: string): string {
  const base = apiUrl || (typeof window !== 'undefined' ? window.location.origin : '')
  // Legacy old-style HF Space naming (kept for back-compat).
  if (base.includes('find-care-chat')) {
    return base.replace('find-care-chat', 'shared-services')
  }
  // Current HF Space naming: skipsnow-{prefix}chathealthyspace.hf.space ->
  // skipsnow-{prefix}sharedservicesspace.hf.space.
  if (base.includes('chathealthyspace.hf.space')) {
    return base.replace('chathealthyspace.hf.space', 'sharedservicesspace.hf.space')
  }
  // Local: SS on :8002 regardless of incoming port (FC :7860, Caddy :443).
  if (base.includes('localhost')) {
    // No regex per Rule-008 #4. Strip any :port suffix, then re-add :8002.
    const slashSlash = base.indexOf('//')
    const hostStart = slashSlash >= 0 ? slashSlash + 2 : 0
    const pathStart = base.indexOf('/', hostStart)
    const hostPort = pathStart === -1 ? base.slice(hostStart) : base.slice(hostStart, pathStart)
    const colon = hostPort.indexOf(':')
    const host = colon === -1 ? hostPort : hostPort.slice(0, colon)
    return base.slice(0, hostStart) + host + ':8002' + (pathStart === -1 ? '' : base.slice(pathStart))
  }
  return base
}

// ── Session-expiry pair tracker (REQ-T-005/T-006/T-008) ──────────
// Browser keeps (time_remaining_seconds, receivedAt) plus the
// server-derived was_registered flag, mirroring the wrapper. Updated
// on every /gate response in this iframe. Never reads user_object
// (which no longer crosses the wire per REQ-T-008).
let _sessionExpiry: { remaining: number; receivedAt: number } | null = null
let _wasRegistered = false

function _noteGateResponse(payload: any): void {
  if (!payload || typeof payload !== 'object') return
  if (typeof payload.time_remaining_seconds === 'number') {
    _sessionExpiry = { remaining: payload.time_remaining_seconds, receivedAt: Date.now() }
  }
  if (typeof payload.was_registered === 'boolean') {
    _wasRegistered = payload.was_registered
  }
}

function _sessionRemainingSeconds(): number | null {
  if (!_sessionExpiry) return null
  return _sessionExpiry.remaining - (Date.now() - _sessionExpiry.receivedAt) / 1000
}

function _isSessionExpired(): boolean {
  const r = _sessionRemainingSeconds()
  return r !== null && r <= 0
}

function _handleSessionExpired(): void {
  // REQ-T-010: hand the page back to the wrapper so the home-page notice
  // renders at the wrapper's origin (where ChatHealthyRegisteredUserCookie
  // would land in the HttpOnly browser-side store).
  const who = _wasRegistered ? 'registered' : 'guest'
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'session-expired', was_registered: _wasRegistered }, '*')
    }
  } catch { /* iframe parent inaccessible */ }
  // Navigate the iframe to a stub /timed-out marker so any test inspecting
  // the iframe document state sees the expiry was honored.
  try {
    window.location.assign(window.location.pathname + '?system_timed_out=' + who)
  } catch { /* defensive */ }
}

// Boot the iframe's session: POST /gate op="boot" on mount, populate the
// stored pair, refuse any subsequent SS call when expired, and capture
// the cryptographic-display projection of the session token so
// handleEvaluate can carry it on the cross-service /evaluate/providers
// POST. (S-005 + S-003-REQ-B-004 contract: the SessionToken IS on the
// wire; the full user_object is NOT.)
async function _ifameGateBoot(): Promise<void> {
  const ssUrl = _sharedServicesUrl(API_URL)
  // Ask the parent for its session token. If the parent has already
  // booted, its handler answers via auth:boot postMessage and the
  // unconditional listener (module-scope) sets _sessionToken before our
  // own boot fetch fires. If the parent hasn't booted yet, we proceed
  // without prior_guid (a stray fresh session is minted; the listener
  // overrides _sessionToken once the parent broadcasts, so subsequent
  // utterance calls land on the parent's session).
  if (typeof window !== 'undefined' && window.parent !== window && !_sessionToken) {
    try { window.parent.postMessage({ type: 'auth:boot-request' }, '*') } catch { /* parent inaccessible */ }
  }
  const body: any = { op: 'boot' }
  // SessionToken.token = "CH" + nonce(35) + guid(32); the trailing 32
  // chars are the GUID. The model has no `guid` field directly.
  const tok = _sessionToken && (_sessionToken as any).token
  if (tok && tok.length >= 32) body.prior_guid = tok.slice(-32)
  const resp = await fetch(`${ssUrl}/gate`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (resp.status >= 500 || (resp.status >= 400 && resp.status !== 401 && resp.status !== 403)) {
    sendToParent('gui:fatal-error', { message: 'authentication error from SharedServices' })
    throw new Error(`gate boot HTTP ${resp.status}`)
  }
  let payload: any
  try { payload = await resp.json() } catch {
    sendToParent('gui:fatal-error', { message: 'authentication error from SharedServices' })
    throw new Error('gate boot malformed body')
  }
  _noteGateResponse(payload)
  // If the unconditional auth:boot listener has already pushed the
  // parent's session token, keep it — that is the page-session GUID we
  // need to share. The iframe's own boot response is a stray fresh
  // session minted because prior_guid was empty when this request
  // raced ahead of the parent's push; it is harmless and TTL-evicted.
  if (payload && payload.session_token && !_sessionToken) {
    _sessionToken = payload.session_token
  }
}

// ── Session token (cryptographic-display projection) ─────────────
// The page-session GUID belongs to the parent. Per S-012-REQ-T-003 the
// GUID is stable per session, so the iframe MUST share the parent's
// SessionToken — never mint independently. The parent broadcasts the
// token over auth:boot postMessage as soon as it has it; the iframe
// also receives it on its own /gate boot response above.
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
    try { window.parent.postMessage({ type: 'auth:boot-request' }, '*') } catch { /* parent inaccessible */ }
    setTimeout(() => { window.removeEventListener('message', handler); resolve(null) }, timeoutMs)
  })
}

async function getSessionToken(): Promise<any> {
  if (_sessionToken) return _sessionToken
  const parentTok = await _requestParentAuth(10000)
  if (parentTok) {
    _sessionToken = parentTok
    return parentTok
  }
  // Standalone iframe (parent === window in dev harness): boot ourselves.
  await _ifameGateBoot()
  return _sessionToken
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

// ── Main Component ───────────────────────────────────────────────
export default function FindCareApp() {
  const [phase, setPhase] = useState<Phase>('welcome')
  const [question, setQuestion] = useState('')
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
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

  // EPIC-002-F-003-S-005-REQ-T-008: every page load of any ChatHealthy.ai
  // client surface MUST POST /gate op="boot" as the FIRST SharedServices
  // network call. Do this on mount before fetching /welcome so the
  // (time_remaining_seconds, Date.now()) pair is populated before any
  // subsequent SS request (REQ-T-005).
  useEffect(() => {
    // The unconditional auth:boot listener (module-scope) pushes the
    // parent's session token into _sessionToken as soon as the parent
    // broadcasts. _ifameGateBoot reads _sessionToken and rides
    // prior_guid in the /gate body so the cookie-dropped localhost path
    // still resumes the parent's admin.sessions record.
    _ifameGateBoot()
      .catch(err => {
        console.warn('[FindCareApp] gate boot failed:', err && err.message)
      })
      .finally(() => {
        fetch(`${API_URL}/welcome`, { method: 'POST' })
          .then(r => r.json())
          .then(d => setWelcomeHtml(d.message || 'Welcome to ChatHealthy FindCare'))
          .catch(() => setWelcomeHtml('Welcome to ChatHealthy FindCare'))
      })
  }, [])

  // Keep refs in sync for closure access
  searchParamsRef.current = searchParams
  questionRef.current = question

  // FC-EVAL-001: Send selection count to parent for evaluate button cold/hot state
  useEffect(() => {
    sendToParent('gui:selection-count', { count: selection.state.selected.length, max: selection.state.maxSelected })
  }, [selection.state.selected.length])

  // Listen for parent page events (filter apply, evaluate click)
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object') return

      // Bug 5: parent asks for the current selection count after returning
      // to FindCare from EvaluateCare so it can re-render the Evaluate
      // button. Reply with whatever the live selection holds.
      if (msg.type === 'gui:request-selection-count') {
        sendToParent('gui:selection-count', {
          count: selectedRef.current.length,
          max: selection.state.maxSelected,
        })
        return
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
        if (msg.action === 'filter-apply' && searchParamsRef.current) {
          // EPIC-006-F-002-S-001-REQ-B-001 submission rule: the codes submitted
          // are the SpecialtyFilter's currently-checked rows. Fall back to
          // msg.value for back-compat with the parent's legacy HTML panel
          // until the parent stops sending it.
          let codes: string[] = checkedCodesRef.current
          if (!codes.length && msg.value) {
            try { codes = JSON.parse(msg.value) } catch { codes = [] }
          }
          const params = { ...searchParamsRef.current, specialty_codes: codes }
          // EPIC-006-F-002-S-001-REQ-B-001 / S-004-REQ-T-001/T-002:
          //   clear unpicked providers, start two timers, preserve selected
          //   and the prompt. fetchProviders' .then() resets the state.
          selection.setAvailable([])
          setReclassifying(true)
          setThinkSeconds(0)
          if (timerRef.current) clearInterval(timerRef.current)
          const start = Date.now()
          timerRef.current = setInterval(() => {
            const elapsed = Math.round((Date.now() - start) / 1000)
            setThinkSeconds(elapsed)
            sendToParent('gui:timer', { seconds: elapsed })
          }, 1000)
          fetchProviders(params, questionRef.current).finally(() => {
            if (timerRef.current) {
              clearInterval(timerRef.current)
              timerRef.current = null
            }
            sendToParent('gui:timer-clear')
            setReclassifying(false)
            // Re-emit the LIVE selection count (selection.state.selected
            // is captured by useEffect's mount-time closure and goes stale;
            // selectedRef is updated on every render).
            sendToParent('gui:selection-count', {
              count: selectedRef.current.length,
              max: selection.state.maxSelected,
            })
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
    selection.flushGarbage()

    const start = Date.now()
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      const elapsed = Math.round((Date.now() - start) / 1000)
      setThinkSeconds(elapsed)
      sendToParent('gui:timer', { seconds: elapsed })
    }, 1000)

    const finishTimer = () => {
      if (timerRef.current) clearInterval(timerRef.current)
      timerRef.current = null
      sendToParent('gui:timer-clear')
    }

    let sawError = false

    const onSpecialties = (data: any) => {
      if (data.error || !data.specialties?.length) {
        // No reasonable query (e.g., "fiddlesticks"). This is a soft
        // failure, not an infrastructure outage — show an inline error
        // in the chat phase, NOT the full-screen 503 overlay.
        finishTimer()
        const msg = data.error
          || "I couldn't match your question to any medical specialty. Try rephrasing — for example, \"find me a bone doctor in Delaware\"."
        setError(msg)
        setPhase('error')
        sawError = true
        return
      }
      const specMap: Record<string, string> = {}
      data.specialties.forEach((s: any) => { specMap[s.code] = s.name })
      specialtyMapRef.current = specMap

      const codes = data.specialties.map((s: any) => s.code)
      const params: any = { specialty_codes: codes, limit: 25 }
      if (data.state) params.state = data.state
      if (data.city) params.city = data.city
      if (data.county) params.county = data.county
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

    const onProviders = (data: any) => {
      if (ac.signal.aborted) return
      if (data.error) {
        // Provider search returned a soft error (timeout, no matches).
        // Inline error in the chat phase — NOT the full-screen 503 overlay.
        finishTimer()
        const msg = data.error.startsWith('search_unavailable')
          ? "I couldn't find providers for that query. Try a more specific search like \"find me a bone doctor in Delaware\"."
          : data.error
        setError(msg)
        setPhase('error')
        sawError = true
        return
      }
      if (data.providers) {
        const enriched = data.providers.map((p: any) => ({
          ...p,
          specialty: specialtyMapRef.current[p.taxonomy_code] || '',
        }))
        selection.setAvailable(enriched as Provider[])
        if (data.total_count) setTotalCount(data.total_count)
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore((data.providers.length || 0) < (data.total_count || 0))
      }
      finishTimer()
      setPhase('results')
    }

    try {
      // REQ-T-006 pre-flight before issuing any SS request.
      if (_isSessionExpired()) {
        _handleSessionExpired()
        return
      }
      const ssUrl = _sharedServicesUrl(API_URL)
      const resp = await fetch(`${ssUrl}/gate`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
        },
        body: JSON.stringify(((): any => {
          const b: any = { op: 'utterance', payload: { text } }
          const tok = _sessionToken && (_sessionToken as any).token
          if (tok && tok.length >= 32) b.prior_guid = tok.slice(-32)
          return b
        })()),
        signal: ac.signal,
      })
      if (resp.status >= 500 || (resp.status >= 400 && resp.status !== 401 && resp.status !== 403)) {
        sendToParent('gui:fatal-error', { message: 'authentication error from SharedServices' })
        throw new Error(`gate stream HTTP ${resp.status}`)
      }
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
          // REQ-T-005: every /gate response refreshes the stored pair.
          _noteGateResponse(evt)
          if (evt.kind === 'specialties') onSpecialties(evt.data || {})
          else if (evt.kind === 'providers') onProviders(evt.data || {})
          else if (evt.kind === 'final' && !sawError && evt.ok === false) {
            // Server reported a soft failure (no specialties matched, etc.).
            // Inline error in the chat — NOT the full-screen 503 overlay.
            finishTimer()
            const msg = evt.error === 'no_specialties'
              ? "I couldn't match your question to any medical specialty. Try rephrasing — for example, \"find me a bone doctor in Delaware\"."
              : (evt.error || 'Search failed')
            setError(msg)
            setPhase('error')
            sawError = true
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
      `<div style="display:block;padding: 1em;border-bottom:0.125em solid #f0f0f0;" data-spec-code="${opt.code}" data-can-prescribe="${opt.can_prescribe || false}" data-homeopathic="${opt.homeopathic || false}">
        <label style="display:flex;align-items:center;gap:0.75em;cursor:pointer;font-size: 1em;">
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
            <div style="padding: 1em;border-bottom:0.25em solid #0b7a75;background:#f8fffe;height:100%;box-sizing:border-box;">
              <!-- EPIC-006-F-002-S-001-REQ-B-008: 7 elements in order inside cell 1 (green header):
                   (1) Filter by specialty label, (2) All possible, (3) Prescribers count,
                   (4) Your choices, (5) Uncheck All toggle, (6) Prescribers checkbox,
                   (7) Homeopathic checkbox. Uncheck All sits to the LEFT of the checkbox column. -->
              <div style="display:flex;align-items:center;flex-wrap:wrap;gap:0.5em 0;">
                <div style="flex:0 0 auto;padding-right: 1em;border-right:0.125em solid #d8e2e1;">
                  <div style="font-size: 1em;font-weight:700;color:#0b7a75;text-transform:uppercase;white-space:nowrap;">Filter by specialty</div>
                </div>
                <div style="flex:0 0 auto;padding: 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size: 1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">All possible</div>
                  <div style="font-size: 1em;font-weight:700;color:#1f2937;">${allCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size: 1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Prescribers</div>
                  <div style="font-size: 1em;font-weight:700;color:#1f2937;" id="filterFilteredCount">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: 1em;border-right:0.125em solid #d8e2e1;text-align:center;">
                  <div style="font-size: 1em;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Your choices</div>
                  <div style="font-size: 1em;font-weight:700;color:#0b7a75;" id="filterShowing">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding: 1em;border-right:0.125em solid #d8e2e1;display:flex;align-items:center;">
                  <button data-gui-action="toggle-all"
                    style="background:#fff;border:0.125em solid #0b7a75;border-radius:0.375em;padding: 1em;font-size: 1em;color:#0b7a75;cursor:pointer;font-weight:600;white-space:nowrap;">Uncheck All</button>
                </div>
                <div style="flex:0 0 auto;padding-left: 1em;display:flex;flex-direction:column;gap:0.375em;">
                  <label style="font-size: 1em;color:#1f2937;display:flex;align-items:center;gap:0.5em;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="prescribers" checked
                      style="accent-color:#0b7a75;width:1.625em;height:1.625em;" /> Prescribers
                  </label>
                  <label style="font-size: 1em;color:#1f2937;display:flex;align-items:center;gap:0.5em;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="homeopathic"
                      style="accent-color:#0b7a75;width:1.625em;height:1.625em;" /> Homeopathic
                  </label>
                </div>
              </div>
            </div>
          </div>
          <div data-cell="2" style="flex:0 0 40%;max-height:22em;overflow-y:auto;overflow-x:hidden;">${items}</div>
          <div data-cell="3" style="flex:0 0 20%;padding: 1em;border-top:0.125em solid #d8e2e1;box-sizing:border-box;overflow:hidden;">
            <button data-gui-action="filter-apply" style="width:100%;padding: 1em;border-radius:0.5em;border:none;background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;font-size: 1em;font-weight:600;cursor:pointer;">Apply Filter</button>
          </div>
          <div data-cell="4" id="guiSessionCell" style="flex:0 0 22%;padding: 1em;border-top:0.125em solid #e5e7eb;box-sizing:border-box;overflow:hidden;"></div>
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
  // EvaluateCare's /evaluate/providers requires a signed SessionToken
  // (it is the cross-service mutual-auth handshake). The parent owns
  // the canonical session token under S-005 — we obtain it via the
  // auth:boot postMessage shim (with a fallback to a direct /gate boot
  // when the iframe is running standalone in the dev harness).
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
      alert(`EvaluateCare error: ${err.message}`)
    }
  }, [question])

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

  // ── Render ─────────────────────────────────────────────────────
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>

      {/* WELCOME PHASE */}
      {phase === 'welcome' && (
        <div style={{ flex: 1, overflowY: 'auto', padding: 1em}}>
          <div style={{
            padding: 1em
            border: '0.125em solid #e5e7eb', fontSize: '1em', lineHeight: 1.6,
          }} dangerouslySetInnerHTML={{ __html: welcomeHtml }} />
        </div>
      )}

      {/* SEARCHING PHASE */}
      {phase === 'searching' && (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12 }}>
          <div style={{ fontSize: '1em', color: '#6b7280' }}>Searching for: <strong>{question}</strong></div>
          <div style={{ fontSize: '1em', color: '#0b7a75', fontWeight: 700 }}>{thinkSeconds}s</div>
          <div style={{ fontSize: '1em', color: '#9ca3af' }}>Waiting for response...</div>
        </div>
      )}

      {/* ERROR PHASE */}
      {phase === 'error' && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 1em}}>
          <div style={{ padding: 1em}}>
            <strong>Error:</strong> {error}
          </div>
        </div>
      )}

      {/* RESULTS PHASE */}
      {phase === 'results' && (
        <>
          {/* Question bar */}
          <div style={{
            padding: 1em
            fontSize: '1em', color: '#0b7a75', fontWeight: 600,
          }}>
            {question}
            <span style={{ float: 'right', fontWeight: 400, color: '#6b7280', fontSize: '1em' }}>
              {totalCount} providers found
            </span>
          </div>

          {/* SpecialtyFilter is now hosted in the parent's leftPanel
              (legacy HTML render of the 4-cell grid). React component
              version disabled inside the iframe until placement is
              resolved at the architecture layer. */}

          {/* Available providers — scrollable top half */}
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }} data-testid="available-providers">
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: 1em
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
                style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: 1em}}
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
                selectionFull={selection.isFull}
              />
            ))}

            {!reclassifying && hasMore && (
              <div style={{ padding: 1em}}>
                <button
                  onClick={loadMore}
                  disabled={isLoadingMore}
                  style={{
                    padding: 1em
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
              padding: 1em
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span style={{ fontSize: '1em', fontWeight: 600, color: '#d97706', textTransform: 'uppercase' }}>
                Selected for Evaluation
              </span>
              <span style={{ fontSize: '1em', color: '#6b7280' }}>
                {selection.state.selected.length} / {selection.state.maxSelected}
              </span>
            </div>

            {selection.state.selected.length === 0 ? (
              <div style={{ padding: 1em}}>
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

      {/* Input bar — always visible.
          EPIC-006-F-002-S-001-REQ-B-001 "Two timers" rule: a small timer is
          shown to the LEFT of the user-prompt input whenever the
          initial-search or re-classify timer is running. */}
      <form onSubmit={handleSend} style={{
        padding: 1em
        background: '#fff',
      }}>
        {(phase === 'searching' || reclassifying) && (
          <div
            data-testid="prompt-row-timer"
            style={{
              flex: '0 0 auto', minWidth: 44, padding: 1em
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
          style={{
            flex: 1, padding: 1em
            border: '0.125em solid #d1d5db', fontSize: '1em', outline: 'none',
            minHeight: 44,
          }}
        />
        <button
          type="submit"
          style={{
            padding: 1em
            background: '#0b7a75',
            color: '#fff', fontSize: '1em', fontWeight: 600, cursor: 'pointer',
            minHeight: 44, minWidth: 44,
          }}
        >Send</button>
      </form>
    </div>
  )
}
