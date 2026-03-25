import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'

interface Message {
  role: 'user' | 'assistant'
  content: string
}

const sanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    span: [['className', 'state-name']],
  },
}

export default function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === 'user'

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
        background: isUser ? '#f3f4f6' : '#ffffff',
        border: isUser ? 'none' : '1px solid #e5e7eb',
        fontSize: 15,
        lineHeight: 1.6,
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
      </div>
    </div>
  )
}
