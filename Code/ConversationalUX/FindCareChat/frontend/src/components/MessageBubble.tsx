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
    a: [...(defaultSchema.attributes?.a || []), 'target', 'rel'],
    th: ['align'],
    td: ['align'],
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

  return (
    <div style={{
      display: 'flex',
      justifyContent: isUser ? 'flex-end' : 'flex-start',
      marginBottom: 12,
    }}>
      <div style={{
        maxWidth: '75%',
        padding: '12px 16px',
        borderRadius: isUser ? '18px 18px 4px 18px' : '18px 18px 18px 4px',
        background: isError ? '#fff5f5' : isUser ? '#f3f4f6' : '#ffffff',
        border: isError ? '1px solid #f87171' : isUser ? 'none' : '1px solid #e5e7eb',
        fontSize: 15,
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
              a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
              table: ({ node, ...props }) => <table {...props} style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0', fontSize: 13, tableLayout: 'auto' }} />,
              th: ({ node, ...props }) => <th {...props} style={{ border: '1px solid #d1d5db', padding: '6px 12px', background: '#f3f4f6', fontWeight: 600, textAlign: 'left', whiteSpace: 'nowrap' }} />,
              td: ({ node, children, ...props }) => {
                const text = String(children || '')
                const isShort = text.length < 15
                return <td {...props} style={{ border: '1px solid #d1d5db', padding: '6px 12px', whiteSpace: isShort ? 'nowrap' : 'normal' }}>{children}</td>
              },
            }}
          >
            {message.content}
          </ReactMarkdown>
        )}
        {!isUser && (message.build || message.thinkSeconds !== undefined || message.tokensIn !== undefined) && (
          <div style={{ marginTop: 6, fontSize: 11, color: '#9ca3af' }}>
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
