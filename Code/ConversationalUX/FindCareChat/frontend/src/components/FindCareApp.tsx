// Copyright (c) 2026 Skip Snow. All rights reserved.
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

const API_URL = import.meta.env.VITE_API_URL ?? ''

type Phase = 'welcome' | 'searching' | 'results' | 'error'

// ── Utility: send postMessage to parent page ─────────────────────
function sendToParent(type: string, data: any = {}) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type, ...data }, '*')
  }
}

// ── Session token ────────────────────────────────────────────────
let _sessionToken: any = null
async function getSessionToken(): Promise<any> {
  if (_sessionToken) return _sessionToken
  try {
    const resp = await fetch(`${API_URL}/session`)
    _sessionToken = await resp.json()
  } catch {
    _sessionToken = { origin: 'FindCare', token: `CH_${crypto.randomUUID()}`, signed: false }
  }
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
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Fetch welcome message on mount
  useEffect(() => {
    fetch(`${API_URL}/welcome`)
      .then(r => r.json())
      .then(d => setWelcomeHtml(d.message || 'Welcome to ChatHealthy FindCare'))
      .catch(() => setWelcomeHtml('Welcome to ChatHealthy FindCare'))
  }, [])

  // Listen for parent page events (filter apply, evaluate click)
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object') return

      if (msg.type === 'gui:event') {
        if (msg.action === 'filter-apply' && searchParams) {
          // Re-search with filter applied
          const params = { ...searchParams, specialty_codes: JSON.parse(msg.value || '[]') }
          fetchProviders(params, question)
        }
        if (msg.action === 'evaluate-providers') {
          handleEvaluate()
        }
      }
    }
    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [searchParams, question])

  // ── Search ─────────────────────────────────────────────────────
  const doSearch = useCallback(async (text: string) => {
    setQuestion(text)
    setPhase('searching')
    setThinkSeconds(0)
    setError('')
    selection.flushGarbage()

    // Start timer
    const start = Date.now()
    timerRef.current = setInterval(() => {
      setThinkSeconds(Math.round((Date.now() - start) / 1000))
    }, 1000)

    // Send timer to parent control frame
    sendToParent('gui:timer')

    try {
      const resp = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: [] }),
      })
      const data = await resp.json()

      if (timerRef.current) clearInterval(timerRef.current)
      sendToParent('gui:timer-clear')

      if (data.error) {
        setError(data.error)
        setPhase('error')
        return
      }

      // Fetch structured providers
      if (data.pagination?.total_count > 0) {
        setTotalCount(data.pagination.total_count)
        setSearchParams(data.pagination.search_params)
        setHasMore(data.pagination.has_more || false)
        setLastNpi(data.pagination.last_npi || '')

        await fetchProviders(
          { ...data.pagination.search_params, limit: data.pagination.count || 25 },
          text,
        )

        // Send filter options to parent
        if (data.pagination.specialization_options?.length > 1) {
          sendFilterToParent(data.pagination.specialization_options, data.pagination.search_params)
        }
      } else {
        // No providers — show the LLM response as text
        setWelcomeHtml(data.response || 'No providers found.')
        setPhase('welcome')
      }
    } catch (err: any) {
      if (timerRef.current) clearInterval(timerRef.current)
      sendToParent('gui:timer-clear')
      setError(err.message || 'Search failed')
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
      const data = await resp.json()
      if (data.providers) {
        selection.setAvailable(data.providers as Provider[])
        if (data.total_count) setTotalCount(data.total_count)
        if (data.last_npi) setLastNpi(data.last_npi)
        setHasMore((data.providers.length || 0) < (data.total_count || 0))
      }
      setPhase('results')
    } catch {
      setPhase('error')
      setError('Failed to fetch providers')
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
  const sendFilterToParent = useCallback((options: any[], params: any) => {
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

    const html = `
      <div data-filter-panel style="display:flex;flex-direction:column;font-family:system-ui,sans-serif;background:#fff;">
        <div style="padding:8px 10px;border-bottom:2px solid #0b7a75;background:#f8fffe;">
          <div style="display:flex;align-items:center;gap:0;">
            <div style="flex:0 0 auto;padding-right:8px;border-right:1px solid #d8e2e1;">
              <div style="font-size:11px;font-weight:700;color:#0b7a75;text-transform:uppercase;">Filter by specialty</div>
            </div>
            <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
              <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">All possible</div>
              <div style="font-size:13px;font-weight:700;color:#1f2937;">${allCount}</div>
            </div>
            <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
              <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">Prescribers</div>
              <div style="font-size:13px;font-weight:700;color:#1f2937;" id="filterFilteredCount">${prescCount}</div>
            </div>
            <div style="flex:0 0 auto;padding:0 8px;border-right:1px solid #d8e2e1;text-align:center;">
              <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">Your choices</div>
              <div style="font-size:13px;font-weight:700;color:#0b7a75;" id="filterShowing">${prescCount}</div>
            </div>
            <div style="flex:0 0 auto;padding-left:8px;display:flex;flex-direction:column;gap:2px;">
              <label style="font-size:9px;color:#1f2937;display:flex;align-items:center;gap:3px;cursor:pointer;">
                <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="prescribers" checked
                  style="accent-color:#0b7a75;width:12px;height:12px;" /> Prescribers
              </label>
              <label style="font-size:9px;color:#1f2937;display:flex;align-items:center;gap:3px;cursor:pointer;">
                <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="homeopathic"
                  style="accent-color:#0b7a75;width:12px;height:12px;" /> Homeopathic
              </label>
            </div>
          </div>
        </div>
        <div style="padding:3px 10px;border-bottom:1px solid #e5e7eb;background:#fafafa;">
          <button data-gui-action="toggle-all" style="background:none;border:1px solid #0b7a75;border-radius:3px;padding:2px 8px;font-size:10px;color:#0b7a75;cursor:pointer;font-weight:600;">Uncheck All</button>
        </div>
        <div style="max-height:390px;overflow-y:auto;">${items}</div>
        <div style="padding:6px 8px;border-top:1px solid #d8e2e1;">
          <button data-gui-action="filter-apply" style="width:100%;padding:5px;border-radius:4px;border:none;background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;font-size:11px;font-weight:600;cursor:pointer;">Apply Filter</button>
          <button data-gui-action="evaluate-providers" style="width:100%;padding:5px;border-radius:4px;border:none;background:linear-gradient(180deg,#d97706,#b45309);color:#fff;font-size:11px;font-weight:600;cursor:pointer;margin-top:4px;">Evaluate These Providers</button>
        </div>
      </div>`

    sendToParent('gui:filter', { html, searchParams: JSON.stringify(params), applyInitialFilter: true })
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
      const resp = await fetch(`${API_URL}/evaluate/providers`, {
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

            {selection.state.available.map(p => (
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

          {/* Selected providers — sticky bottom half */}
          <div style={{
            borderTop: '2px solid #d97706', background: '#fffdf7',
            maxHeight: '35%', overflowY: 'auto',
          }}>
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
              selection.state.selected.map(p => (
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
