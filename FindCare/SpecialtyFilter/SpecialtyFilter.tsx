// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SpecialtyFilter — view-only. Header is a <table> for predictable column
// layout. Body is a scrollable <table>. Font sizes via window.chFont(scale)
// from the canonical CH_FONTS registry.
import { useEffect, useState } from 'react'
import { useSpecialtyFilterController } from './useSpecialtyFilterController'
import SessionVerification, { SessionToken } from './SessionVerification'

declare global {
  interface Window {
    chFont: (scale: number) => string
  }
}

const f = (scale: number): string =>
  typeof window.chFont === 'function' ? window.chFont(scale) : `${scale}em`

const TEAL = '#0b7a75'
const TEAL_LIGHT_BG = '#e6f5ec'
const TEAL_LIGHT_BORDER = '#c9e0d3'
const ROW_DIVIDER = '#f0f0f0'

export default function SpecialtyFilter() {
  const v = useSpecialtyFilterController()
  const [sessionToken, setSessionToken] = useState<SessionToken | null>(null)

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const msg = event.data
      if (msg && typeof msg === 'object' && msg.type === 'session_token' && msg.session_token) {
        setSessionToken(msg.session_token as SessionToken)
      }
    }
    window.addEventListener('message', onMessage)
    // Ask the parent for the current session token in case verify-token already
    // resolved before this iframe mounted.
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'session_token_request' }, '*')
    }
    return () => window.removeEventListener('message', onMessage)
  }, [])

  if (v.specialties.length === 0) {
    return (
      <div
        style={{
          padding: '1em',
          color: '#6b7280',
          fontFamily: 'system-ui, sans-serif',
          fontSize: f(2),
        }}
      >
        Loading filter…
      </div>
    )
  }

  return (
    <div
      data-filter-frame-root
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        width: '100%',
        maxWidth: '100%',
        overflowX: 'hidden',
        boxSizing: 'border-box',
        background: '#ffffff',
      }}
    >
      <div
        className="specialty-filter"
        data-testid="specialty-filter"
        style={{
          display: 'flex',
          flexDirection: 'column',
          width: '100%',
          flex: '1 1 auto',
          background: '#ffffff',
          boxSizing: 'border-box',
          minHeight: 0,
        }}
      >
          {/* Header table — 3 columns, 3 rows (title / counts / controls) */}
          <table
            className="specialty-filter__header"
            style={{
              width: '100%',
              borderCollapse: 'separate',
              borderSpacing: 0,
              tableLayout: 'fixed',
              background: TEAL_LIGHT_BG,
              borderBottom: `0.25em solid ${TEAL}`,
            }}
          >
            <tbody>
              {/* Row 1 — title spans all columns */}
              <tr className="specialty-filter__title-row">
                <td
                  colSpan={3}
                  className="specialty-filter__title-cell"
                  style={{ padding: '0.6em 0.8em' }}
                >
                  <span
                    className="specialty-filter__title"
                    style={{
                      fontSize: f(1.2),
                      fontWeight: 700,
                      color: TEAL,
                    }}
                  >
                    Choose Specialties
                  </span>
                </td>
              </tr>

              {/* Row 2 — three count cells */}
              <tr className="specialty-filter__count-row">
                {[
                  { testid: 'count-all-possible', label: 'All possible', value: v.allPossibleCount, color: '#1f2937' },
                  { testid: 'count-all-prescribers', label: 'All prescribers', value: v.allPrescribersCount, color: '#1f2937' },
                  { testid: 'count-your-choices', label: 'Your choices', value: v.yourChoicesCount, color: TEAL },
                ].map((c, i) => (
                  <td
                    key={c.testid}
                    className="specialty-filter__count-cell"
                    data-testid={c.testid}
                    style={{
                      padding: '0.35em 0.3em',
                      textAlign: 'center',
                      verticalAlign: 'middle',
                      width: '33.333%',
                      borderLeft: i === 0 ? `0.5em solid ${TEAL_LIGHT_BG}` : `0.25em solid ${TEAL_LIGHT_BG}`,
                      borderRight: i === 2 ? `0.5em solid ${TEAL_LIGHT_BG}` : `0.25em solid ${TEAL_LIGHT_BG}`,
                    }}
                  >
                    <div
                      style={{
                        background: '#ffffff',
                        border: `0.125em solid ${TEAL_LIGHT_BORDER}`,
                        borderRadius: '0.4em',
                        padding: '0.35em 0.2em',
                      }}
                    >
                      <div
                        className="specialty-filter__count-label"
                        style={{
                          fontSize: f(0.9),
                          color: '#4a5568',
                          textTransform: 'uppercase',
                          letterSpacing: '0.02em',
                          lineHeight: 1.1,
                          wordSpacing: '100vw',
                        }}
                      >
                        {c.label}
                      </div>
                      <div
                        className="specialty-filter__count-value"
                        style={{
                          fontSize: f(1.1),
                          fontWeight: 700,
                          color: c.color,
                          lineHeight: 1.1,
                          marginTop: '0.2em',
                        }}
                      >
                        {c.value}
                      </div>
                    </div>
                  </td>
                ))}
              </tr>

              {/* Row 3 — controls: Uncheck All / Prescribers / Homeopathic */}
              <tr className="specialty-filter__controls-row">
                <td
                  style={{
                    padding: '0.5em 0.4em 0.6em 0.8em',
                    verticalAlign: 'middle',
                    width: '33.333%',
                  }}
                >
                  <button
                    type="button"
                    className="specialty-filter__toggle-all-btn"
                    onClick={v.handleToggleAll}
                    disabled={v.uncheckAllDisabled}
                    title={v.uncheckAllDisabled ? 'No specialties are checked' : ''}
                    data-testid="toggle-all-button"
                    style={{
                      width: '100%',
                      background: '#ffffff',
                      border: `0.125em solid ${TEAL}`,
                      borderRadius: '0.4em',
                      fontSize: f(0.9),
                      fontWeight: 700,
                      color: TEAL,
                      cursor: v.uncheckAllDisabled ? 'not-allowed' : 'pointer',
                      padding: '0.5em 0.4em',
                      opacity: v.uncheckAllDisabled ? 0.45 : 1,
                    }}
                  >
                    {v.labelIsCheckAll ? 'Check All' : 'Uncheck All'}
                  </button>
                </td>
                <td
                  style={{
                    padding: '0.5em 0.4em 0.6em 0.4em',
                    verticalAlign: 'middle',
                    width: '33.333%',
                  }}
                >
                  <label
                    className="specialty-filter__macro-checkbox"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.4em',
                      color: '#1f2937',
                      cursor: 'pointer',
                      userSelect: 'none',
                      fontSize: f(0.9),
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={v.prescribersChecked}
                      onChange={() => v.handlePrescribersToggle()}
                      data-testid="macro-prescribers"
                      style={{ width: '1.2em', height: '1.2em', accentColor: TEAL, margin: 0 }}
                    />
                    Prescribers
                  </label>
                </td>
                <td
                  style={{
                    padding: '0.5em 0.8em 0.6em 0.4em',
                    verticalAlign: 'middle',
                    width: '33.333%',
                  }}
                >
                  <label
                    className="specialty-filter__macro-checkbox"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.4em',
                      color: '#1f2937',
                      cursor: 'pointer',
                      userSelect: 'none',
                      fontSize: f(0.9),
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={v.homeopathicChecked}
                      onChange={() => v.handleHomeopathicToggle()}
                      data-testid="macro-homeopathic"
                      style={{ width: '1.2em', height: '1.2em', accentColor: TEAL, margin: 0 }}
                    />
                    Homeopathic
                  </label>
                </td>
              </tr>
            </tbody>
          </table>

          {/* Body — scrollable list of specialty rows (still a table) */}
          <div
            className="specialty-filter__body"
            data-testid="specialty-list"
            style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}
          >
            <table
              style={{
                width: '100%',
                borderCollapse: 'separate',
                borderSpacing: 0,
                tableLayout: 'fixed',
              }}
            >
              <tbody>
                {v.sortedRows.map(s => (
                  <tr
                    key={s.code}
                    className="specialty-filter__row"
                    data-spec-code={s.code}
                    data-can-prescribe={s.can_prescribe ? 'true' : 'false'}
                    data-homeopathic={s.homeopathic ? 'true' : 'false'}
                    data-homeopathic-general={s.homeopathic_general ? 'true' : 'false'}
                    onClick={() => v.toggleRow(s.code)}
                    role="checkbox"
                    aria-checked={!!v.checked[s.code]}
                    tabIndex={0}
                    onKeyDown={e => {
                      if (e.key === ' ' || e.key === 'Enter') {
                        e.preventDefault()
                        v.toggleRow(s.code)
                      }
                    }}
                    style={{ cursor: 'pointer' }}
                  >
                    <td
                      style={{
                        padding: '0.1em 0.8em',
                        borderBottom: `0.125em solid ${ROW_DIVIDER}`,
                        color: '#1f2937',
                        fontSize: f(0.8),
                        lineHeight: 1.15,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      <span className="specialty-filter__row-name">{s.name}</span>
                    </td>
                    <td
                      style={{
                        padding: '0.1em 0.8em',
                        borderBottom: `0.125em solid ${ROW_DIVIDER}`,
                        textAlign: 'right',
                        width: '2.4em',
                      }}
                    >
                      <input
                        type="checkbox"
                        className="specialty-filter__row-checkbox"
                        checked={!!v.checked[s.code]}
                        readOnly
                        aria-label={s.name}
                        tabIndex={-1}
                        style={{
                          width: '1.2em',
                          height: '1.2em',
                          accentColor: TEAL,
                          margin: 0,
                        }}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Apply Filter row — inside the widget, green border-top mirrors the header borderBottom */}
          <div
            style={{
              flex: '0 0 auto',
              padding: '0.5em 0.8em',
              borderTop: `0.25em solid ${TEAL}`,
              boxSizing: 'border-box',
              position: 'relative',
            }}
          >
        <button
          type="button"
          data-testid="apply-filter-button"
          data-dirty={v.isDirty ? 'true' : 'false'}
          onClick={v.handleApplyClick}
          style={{
            width: '100%',
            padding: '0.4em 0.6em',
            borderRadius: '0.5em',
            border: v.isDirty ? 'none' : '0.125em solid #cbd5d5',
            background: v.isDirty
              ? `linear-gradient(180deg, #0b9a94, ${TEAL})`
              : '#e5e7eb',
            color: v.isDirty ? '#fff' : '#6b7280',
            fontSize: f(1),
            fontWeight: 700,
            cursor: v.isDirty ? 'pointer' : 'not-allowed',
            minHeight: '2.4em',
          }}
        >
          Apply Filter
        </button>

        {v.popupOpen && (
          <div
            data-testid="apply-filter-no-changes-popup"
            role="dialog"
            aria-modal="false"
            style={{
              position: 'absolute',
              bottom: '100%',
              left: '0.8em',
              right: '0.8em',
              marginBottom: '1em',
              padding: '1em',
              borderRadius: '0.6em',
              background: '#fff',
              border: `0.125em solid ${TEAL}`,
              boxShadow: '0 0.25em 1em rgba(0,0,0,0.15)',
              fontFamily: 'system-ui, sans-serif',
              fontSize: f(1),
              color: '#1f2937',
              zIndex: 10,
            }}
          >
            <div style={{ marginBottom: '1em', lineHeight: 1.4 }}>
              {v.noChangesText}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                data-testid="apply-filter-no-changes-popup-ok"
                onClick={v.closePopup}
                style={{
                  padding: '0.4em 0.8em',
                  borderRadius: '0.4em',
                  border: 'none',
                  background: TEAL,
                  color: '#fff',
                  fontSize: f(1),
                  fontWeight: 700,
                  cursor: 'pointer',
                }}
              >
                OK
              </button>
            </div>
          </div>
        )}
      </div>
      </div>

      {/* Session-verification — React component, rendered when parent posts the token */}
      {sessionToken && <SessionVerification token={sessionToken} />}
    </div>
  )
}
