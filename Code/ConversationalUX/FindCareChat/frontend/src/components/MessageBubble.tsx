import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import type { Message } from './ChatWindow'

const sanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    span: [['className', 'state-name']],
  },
}

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
            rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
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
