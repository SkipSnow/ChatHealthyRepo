import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  isError?: boolean
  thinkSeconds?: number
  tokensIn?: number
}

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const WELCOME: Message = {
  role: 'assistant',
  content: [
    '**Welcome to ChatHealthy FindCare**\n\n',
    "Here's what I can help you with:\n\n",
    '- **Identify the right specialty** — not sure what kind of doctor you need? Describe your situation\n',
    '- **Clinical trials** — find recruiting research studies for any condition\n',
    '- **About ChatHealthy** — our mission, team, and platform\n\n',
    'If you think you may be having a medical emergency, tell me right away.\n\n',
    '**What can I help you with today?**',
  ].join(''),
}

export default function ChatWindow() {
  const [messages, setMessages] = useState<Message[]>([WELCOME])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isLocked, setIsLocked] = useState(false)
  const [thinkSeconds, setThinkSeconds] = useState(0)
  const [thinkingDismissed, setThinkingDismissed] = useState(false)

  // Ref so async callbacks always see the latest messages for history
  const messagesRef = useRef<Message[]>([WELCOME])
  useEffect(() => { messagesRef.current = messages }, [messages])

  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  // Think timer — counts up while loading and not dismissed
  useEffect(() => {
    if (!isLoading || thinkingDismissed) {
      setThinkSeconds(0)
      return
    }
    const timer = setInterval(() => setThinkSeconds(s => s + 1), 1000)
    return () => clearInterval(timer)
  }, [isLoading, thinkingDismissed])

  const showThinking = isLoading && !thinkingDismissed
  // Submit is allowed unless locked, or loading and not yet dismissed
  const canSubmit = !isLocked && (!isLoading || thinkingDismissed)

  async function handleSend(e?: React.FormEvent) {
    e?.preventDefault()
    const text = input.trim()
    if (!text || !canSubmit) return

    // Capture history at call time from ref — safe even if a prior call is still in flight
    const historyForBackend = messagesRef.current
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }))

    const userMessage: Message = { role: 'user', content: text }
    setMessages(prev => [...prev, userMessage])
    setInput('')
    setIsLoading(true)
    setThinkingDismissed(false)
    const startTime = Date.now()

    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: historyForBackend }),
      })
      const data = await res.json()
      const elapsed = Math.round((Date.now() - startTime) / 1000)

      if (data.error) {
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
      }
    } catch {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: '**Error:** Could not reach the server. Please try again.',
        isError: true,
      }])
    } finally {
      setIsLoading(false)
      setThinkingDismissed(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
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

        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSend} style={{ padding: '16px', borderTop: '1px solid #e5e7eb', display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          disabled={isLocked}
          placeholder={isLocked ? 'Chat suspended.' : 'Type a message…'}
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
