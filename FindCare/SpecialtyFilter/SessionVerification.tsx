// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SessionVerification — React component rendering the session-token display
// widget. Previously composed in index.html via buildTokenDisplayHtml; now
// lives inside the findcare iframe so font sizing composes with the iframe's
// own font context (chFont) instead of inheriting from the parent page.

declare global {
  interface Window {
    chFont: (scale: number) => string
  }
}

const f = (scale: number): string =>
  typeof window.chFont === 'function' ? window.chFont(scale) : `${scale}em`

const TEAL = '#0b7a75'
const TEAL_LIGHT_BG = '#f0fffe'

export interface SessionToken {
  signature_received: string
  nonce: string
  guid: string
  origin: string
  verified: boolean | string
  server_env: string
  created_at: string
}

interface Props {
  token: SessionToken
}

export default function SessionVerification({ token }: Props) {
  const dataCell = {
    color: '#374151',
    wordBreak: 'break-all' as const,
  }
  return (
    <div
      data-testid="session-verification"
      style={{
        borderTop: `0.25em solid ${TEAL}`,
        background: TEAL_LIGHT_BG,
        padding: '0 0.5em',
      }}
    >
      <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed', fontSize: f(0.7) }}>
        <tbody>
          <tr>
            <td style={{ fontWeight: 700, color: TEAL, textTransform: 'uppercase' }}>
              Session Verification
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b style={{ color: '#6366f1' }}>Signed token:</b>{' '}
              <span style={{ color: '#6366f1' }}>{token.signature_received}</span>
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b style={{ color: '#d97706' }}>Nonce:</b>{' '}
              <span style={{ color: '#d97706' }}>{token.nonce}</span>
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b style={{ color: TEAL }}>GUID:</b>{' '}
              <span style={{ color: TEAL }}>{token.guid}</span>
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b>Origin:</b> {token.origin} &middot; <b>Verified:</b>{' '}
              <span style={{ fontWeight: 700 }}>{String(token.verified)}</span>
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b>Server, serving security token:</b> {token.origin}
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b>Env:</b>{' '}
              <span style={{ color: '#7c3aed', fontWeight: 700 }}>{token.server_env}</span>
            </td>
          </tr>
          <tr>
            <td style={dataCell}>
              <b>Time:</b> {token.created_at}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}
