// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import type { Message } from './ChatWindow'

const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
  ],
  attributes: {
    ...defaultSchema.attributes,
    span: [['className', 'state-name']],
    a: [...(defaultSchema.attributes?.a || []), 'target', 'rel', 'href'],
    th: ['align'],
    td: ['align'],
  },
  protocols: {
    ...defaultSchema.protocols,
    href: [...(defaultSchema.protocols?.href || []), '#action'],
  },
}

// Open all links in new window by default
const linkTarget = ({ node }: any) => ({
  target: '_blank',
  rel: 'noopener noreferrer',
})

export default function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === 'user'
  const isError = message.isError === true

  const handleActionLink = (e: React.MouseEvent<HTMLAnchorElement>, href: string) => {
    e.preventDefault()
    const action = href.replace('#action:', '')
    if (action === 'filter') {
      window.postMessage({ type: 'gui:highlight-filter' }, '*')
    } else if (action === 'next-page') {
      window.postMessage({ type: 'gui:next-page' }, '*')
    }
  }

  return (
    <div style={{
      display: 'flex',
      justifyContent: isUser ? 'flex-end' : 'flex-start',
      marginBottom: '1em',
    }}>
      <div style={{
        maxWidth: '75%',
        padding: '1em',
        borderRadius: isUser ? '2.25em 2.25em 0.5em 2.25em' : '2.25em 2.25em 2.25em 0.5em',
        background: isError ? '#fff5f5' : isUser ? '#f3f4f6' : '#ffffff',
        border: isError ? '0.125em solid #f87171' : isUser ? 'none' : '0.125em solid #e5e7eb',
        fontSize: '1em',
        lineHeight: 1.6,
        wordBreak: 'break-word',
        overflowWrap: 'anywhere',
      }}>
        {isUser ? (
          message.content
        ) : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
            components={{
              a: ({ node, ...props }) => {
                const href = props.href || ''
                if (href.startsWith('#action:')) {
                  return (
                    <a
                      {...props}
                      href={href}
                      onClick={(e) => handleActionLink(e, href)}
                      style={{ color: '#2563eb', cursor: 'pointer', textDecoration: 'underline' }}
                    />
                  )
                }
                return <a {...props} target="_blank" rel="noopener noreferrer" />
              },
              table: ({ node, ...props }) => <table {...props} style={{ borderCollapse: 'collapse', width: '100%', margin: '1em', fontSize: '1em', tableLayout: 'auto' }} />,
              th: ({ node, ...props }) => <th {...props} style={{ border: '0.125em solid #d1d5db', padding: '1em', background: '#f3f4f6', fontWeight: 600, textAlign: 'left', whiteSpace: 'nowrap' }} />,
              td: ({ node, children, ...props }) => {
                const text = String(children || '')
                const isShort = text.length < 15
                return <td {...props} style={{ border: '0.125em solid #d1d5db', padding: '1em', whiteSpace: isShort ? 'nowrap' : 'normal' }}>{children}</td>
              },
            }}
          >
            {message.content}
          </ReactMarkdown>
        )}
        {!isUser && (message.build || message.thinkSeconds !== undefined || message.tokensIn !== undefined) && (
          <div style={{ marginTop: '1em', fontSize: '1em', color: '#9ca3af' }}>
            {[
              message.build ?? null,
              message.thinkSeconds !== undefined ? `${message.thinkSeconds}s` : null,
              message.tokensIn !== undefined ? `${message.tokensIn.toLocaleString()} in` : null,
            ].filter(Boolean).join(' · ')}
          </div>
        )}
      </div>
    </div>
  )
}
