// Copyright (c) 2026 Skip Snow. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).

import { useState, useRef, useEffect, useCallback } from 'react'
import MessageBubble from './MessageBubble'
import { useGUIManager } from './GUIManager'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  isError?: boolean
  isSummary?: boolean
  thinkSeconds?: number
  tokensIn?: number
  build?: string
}

const API_URL = import.meta.env.VITE_API_URL ?? ''
const RETRY_SECONDS = 10
const TIMEOUT_THRESHOLD_SECONDS = 45

const LOADING_MESSAGE: Message = {
  role: 'assistant',
  content: 'Loading...',
}

// Session token — fetched from FindCare backend, signed with x509 cert.
// Client carries the opaque signed blob, passes it on cross-component calls.
let _sessionToken: any = null
async function getSessionToken(apiUrl: string) {
  if (_sessionToken) return _sessionToken
  try {
    const resp = await fetch(`${apiUrl}/session`)
    _sessionToken = await resp.json()
  } catch {
    _sessionToken = { origin: 'FindCare', token: `CH_${crypto.randomUUID()}`, signed: false }
  }
  return _sessionToken
}

export default function ChatWindow() {
  const [messages, setMessages] = useState<Message[]>([LOADING_MESSAGE])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isLocked, setIsLocked] = useState(false)
  const [thinkSeconds, setThinkSeconds] = useState(0)
  const [thinkingDismissed, setThinkingDismissed] = useState(false)
  const [retryCountdown, setRetryCountdown] = useState<number | null>(null)
  const [envBanner, setEnvBanner] = useState<{env: string, build: string, version: string} | null>(null)
  const [showTimeoutModal, setShowTimeoutModal] = useState(false)
  const [timeoutMultiplier, setTimeoutMultiplier] = useState(1)
  const [abandonCount, setAbandonCount] = useState(0)
  const [continueCount, setContinueCount] = useState(0)

  // GUI Manager — non-chat controls (pagination, future widgets)
  const gui = useGUIManager()
  // Pagination: when direction changes (forward/back click from control frame), fetch directly
  useEffect(() => {
    const dir = (gui.pagination as any).direction
    if (!dir || !gui.pagination.visible || !gui.pagination.searchParams) return

    const afterNpi = dir === 'forward' ? gui.pagination.lastNpi : gui.getBeforeNpi()
    const prevPageEnd = gui.pagination.pageEnd
    const prevPageStart = gui.pagination.pageStart
    const pageSize = gui.pagination.pageSize

    // Compute new page position from current state
    const newPageStart = dir === 'forward' ? prevPageEnd + 1 : Math.max(1, prevPageStart - pageSize)

    const params = {
      ...gui.pagination.searchParams,
      after_npi: afterNpi || undefined,
      limit: pageSize,
    }

    setIsLoading(true)
    fetch(`${API_URL}/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    })
      .then(r => r.json())
      .then(data => {
        if (data.providers && data.providers.length > 0) {
          lastProvidersRef.current = data.providers  // Store for EvaluateCare handoff
          const newPageEnd = newPageStart + data.providers.length - 1
          const text = data.providers.map((p: any) =>
            `**${p.name}**\n${p.address}\n${p.county ? p.county : ''}\nPhone: ${p.phone || 'N/A'}\nNPI: ${p.npi}`
          ).join('\n\n')
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `**Records ${newPageStart}\u2013${newPageEnd} of ${gui.pagination.totalCount}:**\n\n${text}`,
          }])
          gui.showPagination(
            gui.pagination.totalCount, newPageStart, newPageEnd,
            data.first_npi, data.last_npi, gui.pagination.searchParams, pageSize,
          )
        }
        setIsLoading(false)
      })
      .catch(() => {
        setMessages(prev => [...prev, { role: 'assistant', content: '**Error:** Could not fetch records.', isError: true }])
        setIsLoading(false)
      })
  }, [(gui.pagination as any).direction, gui.pagination.npiHistory.length])

  // Filter apply — user selected specializations from left panel
  useEffect(() => {
    gui.onFilterApply((codes: string[], params: any) => {
      const searchParams = { ...params, specialty_codes: codes }
      // Remove specialty_query — we have exact codes now
      delete searchParams.specialty_query
      setIsLoading(true)
      fetch(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...searchParams, limit: 25 }),
      })
        .then(r => r.json())
        .then(data => {
          if (data.providers && data.providers.length > 0) {
            const resultText = data.providers.map((p: any) =>
              `**${p.name}**\n${p.address}\n${p.county ? p.county : ''}\nPhone: ${p.phone || 'N/A'}\nNPI: ${p.npi}`
            ).join('\n\n')
            const newMessages: Message[] = [{
              role: 'assistant',
              content: `**Filtered results — ${data.total_count} providers found:**\n\n${resultText}`,
            }]
            if (data.summary_message) {
              newMessages.push({
                role: 'assistant',
                content: data.summary_message,
                isSummary: true,
              } as Message)
            }
            setMessages(prev => [...prev, ...newMessages])
            // Boss design: filtered results show pagination controls immediately
            if (data.has_more) {
              gui.showPagination(
                data.total_count, 1, data.count,
                data.first_npi, data.last_npi, searchParams, data.count,
              )
            }
            // Also store in pending for next-page action link
            pendingPaginationRef.current = {
              totalCount: data.total_count,
              lastNpi: data.last_npi,
              searchParams,
              pageSize: data.count,
              pageEnd: data.count,
            }
          } else {
            setMessages(prev => [...prev, {
              role: 'assistant',
              content: 'No providers found matching the selected specializations.',
            }])
          }
          setIsLoading(false)
        })
        .catch(() => {
          setMessages(prev => [...prev, { role: 'assistant', content: '**Error:** Could not apply filter.', isError: true }])
          setIsLoading(false)
        })
    })
  }, [])

  // FC-RESULT-MSG: handle action links from system summary message
  const fetchNextPageRef = useRef<() => void>(() => {})
  useEffect(() => {
    fetchNextPageRef.current = () => {
      const pp = pendingPaginationRef.current
      if (!pp) return
      pendingPaginationRef.current = null
      setIsLoading(true)
      const pageStart = pp.pageEnd + 1
      fetch(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...pp.searchParams, after_npi: pp.lastNpi, limit: pp.pageSize }),
      })
        .then(r => r.json())
        .then(data => {
          if (data.providers && data.providers.length > 0) {
            const pageEnd = pageStart + data.providers.length - 1
            const resultText = data.providers.map((p: any) =>
              `**${p.name}**\n${p.address}\n${p.county ? p.county : ''}\nPhone: ${p.phone || 'N/A'}\nNPI: ${p.npi}`
            ).join('\n\n')
            setMessages(prev => [...prev, {
              role: 'assistant',
              content: `**Records ${pageStart}\u2013${pageEnd} of ${pp.totalCount}:**\n\n${resultText}`,
            }])
            gui.showPagination(pp.totalCount, pageStart, pageEnd,
              data.first_npi, data.last_npi, pp.searchParams, pp.pageSize)
          }
          setIsLoading(false)
        })
        .catch(() => {
          setMessages(prev => [...prev, { role: 'assistant', content: '**Error:** Could not fetch more results.', isError: true }])
          setIsLoading(false)
        })
    }
  })
  useEffect(() => {
    function handleAction(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'gui:highlight-filter') {
        window.parent.postMessage({ type: 'gui:event', action: 'highlight-filter' }, '*')
      } else if (msg.type === 'gui:next-page') {
        fetchNextPageRef.current()
      }
    }
    window.addEventListener('message', handleAction)
    return () => window.removeEventListener('message', handleAction)
  }, [])

  // Refs — avoid stale closures in async callbacks
  const messagesRef = useRef<Message[]>([LOADING_MESSAGE])
  useEffect(() => { messagesRef.current = messages }, [messages])
  const backendEnvRef = useRef<string>('prod')
  const pendingRetryRef = useRef<{ message: string; history: any[]; startTime: number } | null>(null)
  const pendingPaginationRef = useRef<{
    totalCount: number; lastNpi: string; searchParams: any; pageSize: number; pageEnd: number
  } | null>(null)
  const lastProvidersRef = useRef<any[]>([])  // Store last provider results for EvaluateCare handoff
  const abortControllerRef = useRef<AbortController | null>(null)

  // EvaluateCare handoff — triggered by "Evaluate these providers" button in control frame
  const EVALCARE_URL = import.meta.env.VITE_EVALCARE_URL ?? 'http://localhost:8001'
  useEffect(() => {
    gui.onStopThinking(() => {
      setThinkingDismissed(true)
    })

    gui.onEvaluateProviders(() => {
      const providers = lastProvidersRef.current
      if (!providers || providers.length === 0) return

      setIsLoading(true)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: '**Evaluating providers...**',
      }])

      // Show unencrypted token below the evaluate button
      getSessionToken(API_URL).then(st => {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({
            type: 'gui:session-display',
            token: st.token || '',
          }, '*')
        }
      })

      // Route through FindCare backend proxy (not browser → EvaluateCare direct)
      // FindCare backend forwards to EvaluateCare with mTLS in production
      getSessionToken(API_URL).then(sessionToken => {
        return fetch(`${API_URL}/evaluate/providers`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            providers,
            session_token: sessionToken,
            question_summary: `Provider evaluation requested — ${providers.length} providers`,
          }),
        })
      })
        .then(r => r.json())
        .then(data => {
          if (data.evaluated_providers) {
            // Remove "Evaluating..." message but preserve ALL other chat history
            setMessages(prev => {
              const filtered = prev.filter(m => m.content !== '**Evaluating providers...**')
              // Add confirmation message to chat
              return [...filtered, {
                role: 'assistant' as const,
                content: `**Evaluation sent to EvaluateCare** — ${data.evaluated_providers.length} providers. Check the right panel for results.`,
              }]
            })
            // Send to right panel — use FULL provider data from FindCare, not stripped EvaluateCare response
            if (window.parent && window.parent !== window) {
              window.parent.postMessage({
                type: 'gui:evaluate-result',
                providers: lastProvidersRef.current,
                question: data.question_summary || '',
                session_token: data.session_token || null,
              }, '*')
            }
            // Show session GUID in control frame
            if (window.parent && window.parent !== window && _sessionToken) {
              window.parent.postMessage({
                type: 'gui:session-display',
                token: _sessionToken.token || '',
              }, '*')
            }
          }
          setIsLoading(false)
        })
        .catch(err => {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `**Error:** Could not reach EvaluateCare service. ${err}`,
            isError: true,
          }])
          setIsLoading(false)
        })
    })
  }, [])

  // Fetch welcome message dynamically from server — no static defaults
  useEffect(() => {
    fetch(`${API_URL}/welcome`)
      .then(r => r.json())
      .then(data => {
        if (data.message) {
          setMessages([{ role: 'assistant', content: data.message }])
        }
      })
      .catch(() => {
        setMessages([{ role: 'assistant', content: '**Welcome to ChatHealthy FindCare.** What can I help you with today?' }])
      })
  }, [])

  // Fetch build number + env from /health on mount
  useEffect(() => {
    fetch(`${API_URL}/health`)
      .then(r => r.json())
      .then(data => {
        backendEnvRef.current = data.env || 'prod'
        if (data.env && data.env !== 'prod') {
          setEnvBanner({env: data.env, build: data.build || '?', version: data.version || '?'})
        }
        if (data.build) {
          setMessages(prev => [{ ...prev[0], build: data.build }, ...prev.slice(1)])
        }
      })
      .catch(() => {})
  }, [])

  const bottomRef = useRef<HTMLDivElement>(null)
  const topRef = useRef<HTMLDivElement>(null)
  const welcomeLoadedRef = useRef(false)
  useEffect(() => {
    if (!welcomeLoadedRef.current) {
      // After welcome loads, scroll to top
      topRef.current?.scrollIntoView({ behavior: 'smooth' })
    } else {
      // After user messages, scroll to bottom
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isLoading, retryCountdown])

  // Think timer — counts up while loading, not dismissed, not in retry wait
  useEffect(() => {
    if (!isLoading || thinkingDismissed || retryCountdown !== null) {
      setThinkSeconds(0)
      setShowTimeoutModal(false)
      setTimeoutMultiplier(1)
      // Clear timer from control frame
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'gui:timer-clear' }, '*')
      }
      return
    }
    const timer = setInterval(() => setThinkSeconds(s => s + 1), 1000)
    return () => clearInterval(timer)
  }, [isLoading, thinkingDismissed, retryCountdown])

  // Timeout modal trigger — show at each multiplier of threshold
  useEffect(() => {
    if (!isLoading || thinkingDismissed || showTimeoutModal) return
    if (thinkSeconds > 0 && thinkSeconds >= TIMEOUT_THRESHOLD_SECONDS * timeoutMultiplier) {
      setShowTimeoutModal(true)
    }
  }, [thinkSeconds, isLoading, thinkingDismissed, timeoutMultiplier, showTimeoutModal])

  // Retry countdown tick
  useEffect(() => {
    if (retryCountdown === null || retryCountdown <= 0) return
    const timer = setTimeout(() => setRetryCountdown(c => (c ?? 1) - 1), 1000)
    return () => clearTimeout(timer)
  }, [retryCountdown])

  // Fire retry when countdown reaches 0
  useEffect(() => {
    if (retryCountdown !== 0 || !pendingRetryRef.current) return
    setRetryCountdown(null)
    const { message, history, startTime } = pendingRetryRef.current
    pendingRetryRef.current = null
    doApiCall(message, history, startTime)
  }, [retryCountdown])

  const showThinking = isLoading && !thinkingDismissed && retryCountdown === null
  const canSubmit = !isLoading || thinkingDismissed

  // Send timer to parent control frame
  useEffect(() => {
    if (showThinking && thinkSeconds > 0 && window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'gui:timer', seconds: thinkSeconds }, '*')
    }
  }, [thinkSeconds, showThinking])

  function handleTimeoutContinue() {
    setContinueCount(c => c + 1)
    setShowTimeoutModal(false)
    setTimeoutMultiplier(m => m + 1)
  }

  function handleTimeoutAbandon() {
    setAbandonCount(c => c + 1)
    setShowTimeoutModal(false)
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    setMessages(prev => [...prev, {
      role: 'assistant',
      content: '**Request abandoned.** The response was taking too long. Try a more specific query.',
      isError: true,
      thinkSeconds: thinkSeconds,
    }])
    setIsLoading(false)
    setThinkingDismissed(false)
  }

  async function doApiCall(message: string, history: any[], startTime: number) {
    const controller = new AbortController()
    abortControllerRef.current = controller
    let data: any
    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, history }),
        signal: controller.signal,
      })
      data = await res.json()
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        return // already handled by handleTimeoutAbandon
      }
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: '**Error:** Could not reach the server. Please try again.',
        isError: true,
      }])
      setIsLoading(false)
      setThinkingDismissed(false)
      return
    }
    abortControllerRef.current = null

    const elapsed = Math.round((Date.now() - startTime) / 1000)

    if (data.error) {
      const isRateLimit = data.error_type === 'rate_limit'
      if (isRateLimit && backendEnvRef.current === 'dev') {
        // Dev only: show countdown and auto-retry
        pendingRetryRef.current = { message, history, startTime }
        setRetryCountdown(RETRY_SECONDS)
        return // keep isLoading=true — indicator stays visible
      }
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `**Error:**\n\`\`\`\n${data.error}\n\`\`\``,
        isError: true,
        thinkSeconds: elapsed,
        tokensIn: data.tokens_in ?? undefined,
      }])
    } else {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.response,
        thinkSeconds: elapsed,
        tokensIn: data.tokens_in ?? undefined,
      }])
      if (data.emergency) setIsLocked(true)
      if (data.response === 'Session unlocked.') setIsLocked(false)

      // Store pagination state silently — no controls yet.
      // Controls appear only when user says "yes" and we fetch page 2.
      if (data.pagination?.total_count > 0 && data.pagination?.has_more) {
        pendingPaginationRef.current = {
          totalCount: data.pagination.total_count,
          lastNpi: data.pagination.last_npi,
          searchParams: data.pagination.search_params,
          pageSize: data.pagination.count,
          pageEnd: data.pagination.count,  // first page ends at count (e.g., 25)
        }
        // Fetch first page of structured providers for EvaluateCare handoff
        fetch(`${API_URL}/search`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...data.pagination.search_params, limit: data.pagination.count }),
        })
          .then(r => r.json())
          .then(searchData => {
            if (searchData.providers) {
              lastProvidersRef.current = searchData.providers
            }
          })
          .catch(() => {})  // silent — providers stored for evaluate handoff only
      }

      // Show filter panel if specialization options returned
      if (data.pagination?.specialization_options?.length > 1) {
        gui.showFilterPanel(
          data.pagination.specialization_options,
          data.pagination.search_params,
          data.pagination.total_count || 0,
        )
      }

      // FC-RESULT-MSG / GOV-011: system-built summary message replaces LLM summary
      if (data.pagination?.summary_message) {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.pagination.summary_message,
          isSummary: true,
        } as Message])
      }

      // Clinical trials summary — same GOV-011 pattern
      if (data.trials?.summary_message) {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.trials.summary_message,
          isSummary: true,
        } as Message])
      }
    }

    setIsLoading(false)
    setThinkingDismissed(false)
  }

  async function handleSend(e?: React.FormEvent) {
    e?.preventDefault()
    const text = input.trim()
    if (!text || !canSubmit) return

    // If there's a pending pagination and user responds, fetch page 2 directly (no LLM)
    if (pendingPaginationRef.current) {
      const pp = pendingPaginationRef.current
      pendingPaginationRef.current = null
      setMessages(prev => [...prev, { role: 'user', content: text }])
      setInput('')
      setIsLoading(true)

      // Page position tracked by frontend — backend just returns records
      const pageStart = pp.pageEnd + 1

      fetch(`${API_URL}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...pp.searchParams,
          after_npi: pp.lastNpi,
          limit: pp.pageSize,
        }),
      })
        .then(r => r.json())
        .then(data => {
          if (data.providers && data.providers.length > 0) {
            const pageEnd = pageStart + data.providers.length - 1
            const resultText = data.providers.map((p: any) =>
              `**${p.name}**\n${p.address}\n${p.county ? p.county : ''}\nPhone: ${p.phone || 'N/A'}\nNPI: ${p.npi}`
            ).join('\n\n')
            setMessages(prev => [...prev, {
              role: 'assistant',
              content: `**Records ${pageStart}\u2013${pageEnd} of ${pp.totalCount}:**\n\n${resultText}`,
            }])
            // NOW show controls — user opted in, second page is on screen
            console.log('[ChatWindow] Calling showPagination', { totalCount: pp.totalCount, pageStart, pageEnd })
            gui.showPagination(
              pp.totalCount, pageStart, pageEnd,
              data.first_npi, data.last_npi, pp.searchParams, pp.pageSize,
            )
          }
          setIsLoading(false)
        })
        .catch(() => {
          setMessages(prev => [...prev, { role: 'assistant', content: '**Error:** Could not fetch more results.', isError: true }])
          setIsLoading(false)
        })
      return
    }

    // Hide pagination when user sends a new non-pagination message
    // Filter panel stays open — user may still want to refine
    gui.hidePagination()

    const historyForBackend = messagesRef.current
      .filter(m => (m.role === 'user' || m.role === 'assistant')
        && m.content !== 'Session unlocked.'
        && !m.content.includes('This chat has been suspended')
        && !m.isError)
      .map(m => ({ role: m.role, content: m.content }))

    welcomeLoadedRef.current = true  // first user message — switch to bottom-scroll mode
    setMessages(prev => [...prev, { role: 'user', content: text }])
    setInput('')
    setIsLoading(true)
    setThinkingDismissed(false)
    setShowTimeoutModal(false)
    setTimeoutMultiplier(1)
    setAbandonCount(0)
    setContinueCount(0)

    doApiCall(text, historyForBackend, Date.now())
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
      {envBanner && window.parent === window && (
        <div style={{ background: envBanner.env === 'local' ? '#d97706' : '#dc2626', color: '#fff', textAlign: 'center', padding: '4px 8px', fontSize: 13, fontWeight: 600, letterSpacing: '0.03em' }}>
          {envBanner.env.toUpperCase()} — Build {envBanner.build} — {envBanner.version}
        </div>
      )}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px 16px' }}>
        <div ref={topRef} />
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} />
        ))}

        {/* Timer + Stop moved to parent control frame via gui:timer postMessage */}

        {retryCountdown !== null && (
          <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 12 }}>
            <div style={{
              padding: '10px 14px',
              borderRadius: '18px 18px 18px 4px',
              background: '#fff7ed',
              border: '1px solid #fed7aa',
              fontSize: 14,
              color: '#c2410c',
            }}>
              Rate limit — retrying in {retryCountdown}s
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {showTimeoutModal && (
        <div style={{
          padding: '16px 20px',
          borderTop: '1px solid #fbbf24',
          borderBottom: '1px solid #fbbf24',
          background: '#fffbeb',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
        }}>
          <span style={{ fontSize: 14, color: '#92400e', fontWeight: 500 }}>
            This is taking a long time ({thinkSeconds}s). Do you want to continue?
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={handleTimeoutContinue}
              style={{
                padding: '6px 16px', borderRadius: 6, border: '1px solid #059669',
                background: '#ecfdf5', color: '#059669', fontSize: 13, fontWeight: 600, cursor: 'pointer',
              }}
            >
              Yes, keep waiting
            </button>
            <button
              onClick={handleTimeoutAbandon}
              style={{
                padding: '6px 16px', borderRadius: 6, border: '1px solid #dc2626',
                background: '#fef2f2', color: '#dc2626', fontSize: 13, fontWeight: 600, cursor: 'pointer',
              }}
            >
              No, abandon
            </button>
          </div>
        </div>
      )}

      {/* Control frame lives on the static parent page — React pushes via postMessage */}

      <form onSubmit={handleSend} style={{ padding: '16px', borderTop: '1px solid #e5e7eb', display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder={isLocked ? 'This chat has been suspended for your safety.' : 'Type a message…'}
          style={{ flex: 1, padding: '10px 14px', borderRadius: 8, border: '1px solid #d1d5db', fontSize: 15, outline: 'none' }}
        />
        <button
          type="submit"
          disabled={!canSubmit || !input.trim()}
          style={{
            padding: '10px 20px',
            borderRadius: 8,
            background: canSubmit && input.trim() ? '#003399' : '#d1d5db',
            color: '#fff',
            border: 'none',
            cursor: canSubmit && input.trim() ? 'pointer' : 'not-allowed',
            fontSize: 15,
          }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
