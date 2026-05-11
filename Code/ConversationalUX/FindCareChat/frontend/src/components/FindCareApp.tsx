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
import { useSelectionState } from '../ux/hooks/useSelectionState'
import { ProviderCard } from '../ux/components/ProviderCard'
import type { Provider } from '../ux/types/provider'
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

// ── Session token ────────────────────────────────────────────────
// No client-side fallback. If the server cannot mint a signed token the
// flow MUST fail — synthesizing a CH_{uuid} placeholder here is the kind
// of silent security regression to be avoided.
let _sessionToken: any = null
async function getSessionToken(): Promise<any> {
  const resp = await fetch(`${API_URL}/session`, { method: 'POST' })
  checkSecurityViolation(resp, `${API_URL}/session`)
  _sessionToken = await resp.json()
  return _sessionToken
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

  const selection = useSelectionState()
  const selectedRef = useRef<Provider[]>([])
  selectedRef.current = selection.state.selected
  const searchParamsRef = useRef<any>(null)
  const questionRef = useRef('')
  const specialtyMapRef = useRef<Record<string, string>>({})
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // EPIC-006-F-002-S-001-REQ-B-001: SpecialtyFilter rows (the cached client
  // list — see "Cache Results on client" in REQ-B-001) and a live ref to
  // the codes the user currently has checked, so a parent-driven
  // filter-apply postMessage submits exactly that set.
  const [specialtyRows, setSpecialtyRows] = useState<SpecialtyRecord[]>([])
  const checkedCodesRef = useRef<string[]>([])

  // Fetch welcome message on mount
  useEffect(() => {
    fetch(`${API_URL}/welcome`, { method: 'POST' })
      .then(r => r.json())
      .then(d => setWelcomeHtml(d.message || 'Welcome to ChatHealthy FindCare'))
      .catch(() => setWelcomeHtml('Welcome to ChatHealthy FindCare'))
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
          fetchProviders(params, questionRef.current)
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
  const doSearch = useCallback(async (text: string) => {
    setQuestion(text)
    setPhase('searching')
    setThinkSeconds(0)
    setError('')
    selection.flushGarbage()

    // Start timer — send elapsed seconds to parent control frame every second
    const start = Date.now()
    timerRef.current = setInterval(() => {
      const elapsed = Math.round((Date.now() - start) / 1000)
      setThinkSeconds(elapsed)
      sendToParent('gui:timer', { seconds: elapsed })
    }, 1000)

    try {
      // GOV-011: One AI call to classify, then system queries DB
      // Step 1: AI translates question → structured specialties + location
      const classifyResp = await fetch(`${API_URL}/classify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      checkSecurityViolation(classifyResp, `${API_URL}/classify`)
      const classified = await classifyResp.json()

      if (classified.error || !classified.specialties?.length) {
        if (timerRef.current) clearInterval(timerRef.current)
        sendToParent('gui:timer-clear')
        const msg = classified.error || 'Could not identify relevant specialties'
        // EPIC-008-F-011-S-001-REQ-B-001: fatal errors render full-screen
        // at the parent wrapper (index.html) — not inline in the iframe.
        sendToParent('gui:fatal-error', { message: msg })
        setError(msg)
        setPhase('error')
        return
      }

      // Step 2: System queries DB with AI-provided parameters — no more AI
      // Build taxonomy_code → specialty name lookup from user's selection
      const specMap: Record<string, string> = {}
      classified.specialties.forEach((s: any) => { specMap[s.code] = s.name })
      specialtyMapRef.current = specMap

      const codes = classified.specialties.map((s: any) => s.code)
      const params: any = {
        specialty_codes: codes,
        limit: 25,
      }
      if (classified.state) params.state = classified.state
      if (classified.city) params.city = classified.city
      if (classified.county) params.county = classified.county

      setSearchParams(params)
      await fetchProviders(params, text)

      // Timer clears after DB search completes — not after classify
      if (timerRef.current) clearInterval(timerRef.current)
      sendToParent('gui:timer-clear')

      // Send filter options to parent — use the ranked specialties from AI
      if (classified.specialties.length > 1) {
        const filterOptions = classified.specialties.map((s: any) => ({
          code: s.code,
          name: s.name,
          can_prescribe: s.can_prescribe ?? true,
          homeopathic: s.homeopathic ?? false,
        }))
        // Cache homeopathic generalists for toggle
        const homeoGeneralists = (classified.homeopathic_generalists || []).map((s: any) => ({
          code: s.code,
          name: s.name,
          can_prescribe: s.can_prescribe ?? false,
          homeopathic: true,
          homeopathic_general: true,
        }))
        // EPIC-006-F-002-S-001-REQ-B-001 "Two logical lists":
        //   List one = AI-matched specialties for THIS query (V5 picks)
        //   List two = static homeopathic-generalist fallback set
        // Both are part of the result set the SpecialtyFilter renders.
        // homeopathic_general flag distinguishes list-two members so the
        // component can sort/section them per the spec's [AGENT-FLAG]
        // resolution (default: single merged ranked list with list-one
        // floating above list-two).
        const rows: SpecialtyRecord[] = [
          ...classified.specialties.map(
            (s: any, i: number) => ({
              code: s.code,
              name: s.name,
              can_prescribe: s.can_prescribe ?? true,
              homeopathic: s.homeopathic ?? false,
              homeopathic_general: false,
              rank: typeof s.rank === 'number' ? s.rank : i,
            }),
          ),
          ...(classified.homeopathic_generalists || []).map(
            (s: any, i: number) => ({
              code: s.code,
              name: s.name,
              can_prescribe: s.can_prescribe ?? false,
              homeopathic: true,
              homeopathic_general: true,
              rank: typeof s.rank === 'number' ? s.rank : i,
            }),
          ),
        ]
        setSpecialtyRows(rows)
        sendFilterToParent(filterOptions, params, homeoGeneralists, rows)
      } else {
        setSpecialtyRows([])
      }
    } catch (err: any) {
      if (timerRef.current) clearInterval(timerRef.current)
      sendToParent('gui:timer-clear')
      const msg = err.message || 'Search failed'
      sendToParent('gui:fatal-error', { message: msg })
      setError(msg)
      setPhase('error')
    }
  }, [])

  // ── Fetch providers (search or filter refresh) ─────────────────
  const fetchProviders = useCallback(async (params: any, q: string) => {
    try {
      const resp = await fetch(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      checkSecurityViolation(resp, `${API_URL}/search`)
      const data = await resp.json()
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
    } catch {
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
      const resp = await fetch(`${API_URL}/search`, {
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
      `<div style="display:block;padding:4px 8px;border-bottom:1px solid #f0f0f0;" data-spec-code="${opt.code}" data-can-prescribe="${opt.can_prescribe || false}" data-homeopathic="${opt.homeopathic || false}">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;">
          <input type="checkbox" data-gui-action="filter-toggle" data-gui-value="${opt.code}" checked
            style="accent-color:#0b7a75;width:12px;height:12px;" />
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
            <div style="padding:8px 10px;border-bottom:2px solid #0b7a75;background:#f8fffe;height:100%;box-sizing:border-box;">
              <!-- EPIC-006-F-002-S-001-REQ-B-008: 7 elements in order inside cell 1 (green header):
                   (1) Filter by specialty label, (2) All possible, (3) Prescribers count,
                   (4) Your choices, (5) Uncheck All toggle, (6) Prescribers checkbox,
                   (7) Homeopathic checkbox. Uncheck All sits to the LEFT of the checkbox column. -->
              <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px 0;">
                <div style="flex:0 0 auto;padding-right:8px;border-right:1px solid #d8e2e1;">
                  <div style="font-size:10px;font-weight:700;color:#0b7a75;text-transform:uppercase;white-space:nowrap;">Filter by specialty</div>
                </div>
                <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
                  <div style="font-size:8px;color:#6b7280;text-transform:uppercase;white-space:nowrap;">All possible</div>
                  <div style="font-size:13px;font-weight:700;color:#1f2937;">${allCount}</div>
                </div>
                <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
                  <div style="font-size:8px;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Prescribers</div>
                  <div style="font-size:13px;font-weight:700;color:#1f2937;" id="filterFilteredCount">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
                  <div style="font-size:8px;color:#6b7280;text-transform:uppercase;white-space:nowrap;">Your choices</div>
                  <div style="font-size:13px;font-weight:700;color:#0b7a75;" id="filterShowing">${prescCount}</div>
                </div>
                <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;display:flex;align-items:center;">
                  <button data-gui-action="toggle-all"
                    style="background:#fff;border:1px solid #0b7a75;border-radius:3px;padding:3px 10px;font-size:10px;color:#0b7a75;cursor:pointer;font-weight:600;white-space:nowrap;">Uncheck All</button>
                </div>
                <div style="flex:0 0 auto;padding-left:8px;display:flex;flex-direction:column;gap:3px;">
                  <label style="font-size:10px;color:#1f2937;display:flex;align-items:center;gap:4px;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="prescribers" checked
                      style="accent-color:#0b7a75;width:13px;height:13px;" /> Prescribers
                  </label>
                  <label style="font-size:10px;color:#1f2937;display:flex;align-items:center;gap:4px;cursor:pointer;white-space:nowrap;">
                    <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="homeopathic"
                      style="accent-color:#0b7a75;width:13px;height:13px;" /> Homeopathic
                  </label>
                </div>
              </div>
            </div>
          </div>
          <div data-cell="2" style="flex:0 0 40%;max-height:22em;overflow-y:auto;overflow-x:hidden;">${items}</div>
          <div data-cell="3" style="flex:0 0 20%;padding:6px 8px;border-top:1px solid #d8e2e1;box-sizing:border-box;overflow:hidden;">
            <button data-gui-action="filter-apply" style="width:100%;padding:5px;border-radius:4px;border:none;background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;font-size:11px;font-weight:600;cursor:pointer;">Apply Filter</button>
          </div>
          <div data-cell="4" id="guiSessionCell" style="flex:0 0 22%;padding:4px 8px;border-top:1px solid #e5e7eb;box-sizing:border-box;overflow:hidden;"></div>
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
    sendToParent('gui:session-display', { token: token.token || '' })

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

      // Update left panel with verified token (new nonce from EvaluateCare)
      if (data.session_token) {
        sendToParent('gui:session-display', { session_token: data.session_token })
      }
    } catch (err: any) {
      alert(`EvaluateCare error: ${err.message}`)
    }
  }, [question])

  // ── Handle send ────────────────────────────────────────────────
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
        <div style={{ flex: 1, overflowY: 'auto', padding: '24px 16px', maxWidth: 800, margin: '0 auto', width: '100%' }}>
          <div style={{
            padding: '12px 16px', borderRadius: '18px 18px 18px 4px', background: '#fff',
            border: '1px solid #e5e7eb', fontSize: 15, lineHeight: 1.6,
          }} dangerouslySetInnerHTML={{ __html: welcomeHtml }} />
        </div>
      )}

      {/* SEARCHING PHASE */}
      {phase === 'searching' && (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12 }}>
          <div style={{ fontSize: 14, color: '#6b7280' }}>Searching for: <strong>{question}</strong></div>
          <div style={{ fontSize: 24, color: '#0b7a75', fontWeight: 700 }}>{thinkSeconds}s</div>
          <div style={{ fontSize: 12, color: '#9ca3af' }}>Waiting for response...</div>
        </div>
      )}

      {/* ERROR PHASE */}
      {phase === 'error' && (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20 }}>
          <div style={{ padding: 16, background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 8, color: '#dc2626', maxWidth: 500 }}>
            <strong>Error:</strong> {error}
          </div>
        </div>
      )}

      {/* RESULTS PHASE */}
      {phase === 'results' && (
        <>
          {/* Question bar */}
          <div style={{
            padding: '8px 16px', background: '#f0fffe', borderBottom: '2px solid #0b7a75',
            fontSize: 13, color: '#0b7a75', fontWeight: 600,
          }}>
            {question}
            <span style={{ float: 'right', fontWeight: 400, color: '#6b7280', fontSize: 11 }}>
              {totalCount} providers found
            </span>
          </div>

          {/* SpecialtyFilter is now hosted in the parent's leftPanel
              (legacy HTML render of the 4-cell grid). React component
              version disabled inside the iframe until placement is
              resolved at the architecture layer. */}

          {/* Available providers — scrollable top half */}
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '4px 12px', background: '#fafafa', borderBottom: '1px solid #eee',
            }}>
              <span style={{ fontSize: 10, fontWeight: 600, color: '#0b7a75', textTransform: 'uppercase' }}>
                Available Providers
              </span>
              <span style={{ fontSize: 10, color: '#6b7280' }}>
                {selection.state.available.length} available
                {selection.state.garbage.length > 0 && (
                  <span style={{ color: '#dc2626', marginLeft: 8 }}>🗑 {selection.state.garbage.length}</span>
                )}
              </span>
            </div>

            {selection.state.available.map((p: Provider) => (
              <ProviderCard
                key={p.npi}
                provider={p}
                mode="available"
                onSelect={selection.select}
                onDismiss={selection.dismiss}
                selectionFull={selection.isFull}
              />
            ))}

            {hasMore && (
              <div style={{ padding: 8, textAlign: 'center' }}>
                <button
                  onClick={loadMore}
                  disabled={isLoadingMore}
                  style={{
                    padding: '6px 20px', borderRadius: 4, border: '1px solid #0b7a75',
                    background: '#f0fffe', color: '#0b7a75', fontSize: 12, fontWeight: 600,
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
              borderTop: '2px solid #d97706', background: '#fffdf7',
              minHeight: 60, maxHeight: '35%', overflowY: 'auto', flexShrink: 0,
            }}
            onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
            onDrop={(e) => { e.preventDefault(); const npi = e.dataTransfer.getData('text/plain'); if (npi) selection.select(npi) }}
          >
            <div style={{
              padding: '4px 12px', background: '#fffbeb',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span style={{ fontSize: 10, fontWeight: 600, color: '#d97706', textTransform: 'uppercase' }}>
                Selected for Evaluation
              </span>
              <span style={{ fontSize: 10, color: '#6b7280' }}>
                {selection.state.selected.length} / {selection.state.maxSelected}
              </span>
            </div>

            {selection.state.selected.length === 0 ? (
              <div style={{ padding: 10, textAlign: 'center', color: '#9ca3af', fontSize: 11 }}>
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

      {/* Input bar — always visible */}
      <form onSubmit={handleSend} style={{
        padding: '12px 16px', borderTop: '1px solid #e5e7eb', display: 'flex', gap: 8,
        background: '#fff',
      }}>
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Type a message..."
          style={{
            flex: 1, padding: '10px 14px', borderRadius: 8,
            border: '1px solid #d1d5db', fontSize: 15, outline: 'none',
          }}
        />
        <button
          type="submit"
          disabled={phase === 'searching'}
          style={{
            padding: '10px 20px', borderRadius: 8, border: 'none',
            background: phase === 'searching' ? '#9ca3af' : '#0b7a75',
            color: '#fff', fontSize: 15, fontWeight: 600, cursor: 'pointer',
          }}
        >Send</button>
      </form>
    </div>
  )
}
