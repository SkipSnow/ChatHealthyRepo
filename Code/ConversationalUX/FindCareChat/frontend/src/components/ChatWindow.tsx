import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  isError?: boolean
  thinkSeconds?: number
  tokensIn?: number
  build?: string
}

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'
const RETRY_SECONDS = 10

const DEFAULT_WELCOME = [
  '**Welcome to ChatHealthy FindCare**\n\n',
  "Here's what I can help you with:\n\n",
  '- **Identify the right specialty** — not sure what kind of doctor you need? Describe your situation\n',
  '- **Clinical trials** — find recruiting research studies for any condition\n',
  '- **About ChatHealthy** — our mission, team, and platform\n\n',
  'If you think you may be having a medical emergency, tell me right away.\n\n',
  '**What can I help you with today?**',
].join('')

const WELCOME: Message = {
  role: 'assistant',
  content: DEFAULT_WELCOME,
}

export default function ChatWindow() {
  const [messages, setMessages] = useState<Message[]>([WELCOME])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isLocked, setIsLocked] = useState(false)
  const [thinkSeconds, setThinkSeconds] = useState(0)
  const [thinkingDismissed, setThinkingDismissed] = useState(false)
  const [retryCountdown, setRetryCountdown] = useState<number | null>(null)
  const [envBanner, setEnvBanner] = useState<{env: string, build: string, version: string} | null>(null)

  // Refs — avoid stale closures in async callbacks
  const messagesRef = useRef<Message[]>([WELCOME])
  useEffect(() => { messagesRef.current = messages }, [messages])
  const backendEnvRef = useRef<string>('prod')
  const pendingRetryRef = useRef<{ message: string; history: any[]; startTime: number } | null>(null)

  // Fetch welcome message from API on mount (supports HUMAN_TESTING mode)
  useEffect(() => {
    fetch(`${API_URL}/welcome`)
      .then(r => r.json())
      .then(data => {
        if (data.message) {
          setMessages(prev => [{ ...prev[0], content: data.message }, ...prev.slice(1)])
        }
      })
      .catch(() => {})
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
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading, retryCountdown])

  // Think timer — counts up while loading, not dismissed, not in retry wait
  useEffect(() => {
    if (!isLoading || thinkingDismissed || retryCountdown !== null) {
      setThinkSeconds(0)
      return
    }
    const timer = setInterval(() => setThinkSeconds(s => s + 1), 1000)
    return () => clearInterval(timer)
  }, [isLoading, thinkingDismissed, retryCountdown])

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

  async function doApiCall(message: string, history: any[], startTime: number) {
    let data: any
    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, history }),
      })
      data = await res.json()
    } catch {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: '**Error:** Could not reach the server. Please try again.',
        isError: true,
      }])
      setIsLoading(false)
      setThinkingDismissed(false)
      return
    }

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
    }

    setIsLoading(false)
    setThinkingDismissed(false)
  }

  async function handleSend(e?: React.FormEvent) {
    e?.preventDefault()
    const text = input.trim()
    if (!text || !canSubmit) return

    const historyForBackend = messagesRef.current
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }))

    setMessages(prev => [...prev, { role: 'user', content: text }])
    setInput('')
    setIsLoading(true)
    setThinkingDismissed(false)

    doApiCall(text, historyForBackend, Date.now())
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
      {envBanner && (
        <div style={{ background: envBanner.env === 'local' ? '#d97706' : '#dc2626', color: '#fff', textAlign: 'center', padding: '4px 8px', fontSize: 13, fontWeight: 600, letterSpacing: '0.03em' }}>
          {envBanner.env.toUpperCase()} — Build {envBanner.build} — {envBanner.version}
        </div>
      )}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px 16px' }}>
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} />
        ))}

        {showThinking && (
          <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 12 }}>
            <div style={{
              padding: '10px 14px',
              borderRadius: '18px 18px 18px 4px',
              background: '#f9fafb',
              border: '1px solid #e5e7eb',
              fontSize: 14,
              color: '#6b7280',
              display: 'flex',
              alignItems: 'center',
              gap: 10,
            }}>
              <span>Thinking… {thinkSeconds}s</span>
              <button
                onClick={() => setThinkingDismissed(true)}
                style={{
                  background: 'none',
                  border: '1px solid #d1d5db',
                  borderRadius: 4,
                  padding: '2px 8px',
                  fontSize: 12,
                  color: '#9ca3af',
                  cursor: 'pointer',
                  lineHeight: 1.4,
                }}
              >
                Stop
              </button>
            </div>
          </div>
        )}

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
