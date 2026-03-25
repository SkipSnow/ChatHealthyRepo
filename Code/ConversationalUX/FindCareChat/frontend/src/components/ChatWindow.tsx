import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'

interface Message {
  role: 'user' | 'assistant'
  content: string
}

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const WELCOME: Message = {
  role: 'assistant',
  content: [
    '**Welcome to ChatHealthy FindCare**\n\n',
    "Here's what I can help you with:\n\n",
    '- **Find a doctor** — search for providers by specialty or condition\n',
    '  - <span class="state-name">Delaware</span>\n',
    '  - <span class="state-name">Mississippi</span>\n',
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
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  async function handleSend(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isLoading || isLocked) return

    const userMessage: Message = { role: 'user', content: text }
    const history = [...messages, userMessage]
    setMessages(history)
    setInput('')
    setIsLoading(true)

    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: messages
            .filter(m => m.role === 'user' || m.role === 'assistant')
            .map(m => ({ role: m.role, content: m.content })),
        }),
      })
      const data = await res.json()
      setMessages([...history, { role: 'assistant', content: data.response }])
      if (data.emergency) setIsLocked(true)
    } catch {
      setMessages([...history, { role: 'assistant', content: '**Error:** Could not reach the server. Please try again.' }])
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 800, margin: '0 auto', width: '100%' }}>
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px 16px' }}>
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} />
        ))}
        {isLoading && (
          <div style={{ padding: '12px 16px', color: '#6b7280', fontStyle: 'italic' }}>Thinking…</div>
        )}
        <div ref={bottomRef} />
      </div>
      <form onSubmit={handleSend} style={{ padding: '16px', borderTop: '1px solid #e5e7eb', display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          disabled={isLocked || isLoading}
          placeholder={isLocked ? 'Chat suspended.' : 'Type a message…'}
          style={{ flex: 1, padding: '10px 14px', borderRadius: 8, border: '1px solid #d1d5db', fontSize: 15, outline: 'none' }}
        />
        <button
          type="submit"
          disabled={isLocked || isLoading || !input.trim()}
          style={{ padding: '10px 20px', borderRadius: 8, background: '#003399', color: '#fff', border: 'none', cursor: 'pointer', fontSize: 15 }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
