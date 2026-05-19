// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SpecialtyFilterFrame — the filter-only render of the React app, loaded
// inside the parent's leftPanel as a sub-iframe (?mode=filter). Hosts the
// SpecialtyFilter component + Apply Filter cell + session-verification
// placeholder. Talks to the parent via postMessage; the parent brokers
// state to/from the chat iframe.
//
// Wire summary:
//   chat iframe → parent: gui:filter { specialties: [...], homeopathic_generalists: [...] }
//   parent → filter iframe: gui:filter-data { specialties: [...] }
//   filter iframe → parent → chat iframe: gui:filter-selection-change { codes: [...] }
//   filter iframe → parent → chat iframe: gui:filter-apply-click
import { useEffect, useMemo, useState } from 'react'
import SpecialtyFilter, { type SpecialtyRecord } from './SpecialtyFilter'

// EPIC-006-F-002-S-004-REQ-T-003: the exact message string the spec
// requires the no-changes popup to display. Verbatim — do not paraphrase.
const NO_CHANGES_POPUP_TEXT =
  'There are no changes to apply, try a new prompt if this is the wrong list. ' +
  'The AI agent will help you craft the right question'

function codesKey(codes: string[]): string {
  return [...codes].sort().join('|')
}

export default function SpecialtyFilterFrame() {
  const [specialties, setSpecialties] = useState<SpecialtyRecord[]>([])
  // The set of codes the user last submitted (or the initial default set
  // when the response first lands). Change-state = current ≠ this.
  const [appliedCodes, setAppliedCodes] = useState<string[] | null>(null)
  // The user's current row-checked state, updated on every toggle.
  const [currentCodes, setCurrentCodes] = useState<string[]>([])
  const [popupOpen, setPopupOpen] = useState(false)

  useEffect(() => {
    function handler(e: MessageEvent) {
      const data: any = e.data
      if (!data || typeof data !== 'object') return
      if (data.type === 'gui:filter-data') {
        const incoming = (data.specialties || []) as SpecialtyRecord[]
        setSpecialties(incoming)
        // New result set replaces the cache. Initial applied baseline is
        // the Prescribers-macro-checked default (per REQ-B-001 initial
        // state): every row whose can_prescribe=true is checked. Apply
        // Filter starts greyed because current == applied.
        const initial = incoming
          .filter(s => !!s.can_prescribe)
          .map(s => s.code)
        setAppliedCodes(initial)
        setCurrentCodes(initial)
        setPopupOpen(false)
      }
    }
    window.addEventListener('message', handler)
    window.parent.postMessage({ type: 'gui:filter-ready' }, '*')
    return () => window.removeEventListener('message', handler)
  }, [])

  // EPIC-006-F-002-S-004-REQ-T-003: hot when current ≠ applied; greyed otherwise.
  const isDirty = useMemo(() => {
    if (appliedCodes === null) return false
    return codesKey(currentCodes) !== codesKey(appliedCodes)
  }, [currentCodes, appliedCodes])

  if (specialties.length === 0) {
    return (
      <div style={{ padding: '1em', color: '#6b7280', fontFamily: 'system-ui, sans-serif', fontSize: '1em' }}>
        Loading filter…
      </div>
    )
  }

  return (
    <div
      data-filter-frame-root
      style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: '#ffffff' }}
    >
      {/* SpecialtyFilter — cells 1+2 of the 4-cell left-panel grid */}
      <div style={{ flex: '1 1 58%', minHeight: 0, overflow: 'hidden' }}>
        <SpecialtyFilter
          specialties={specialties}
          onSelectionChange={codes => {
            setCurrentCodes(codes)
            window.parent.postMessage(
              { type: 'gui:filter-selection-change', codes },
              '*',
            )
          }}
          onCloseRequest={() => {
            window.parent.postMessage({ type: 'gui:filter-close-request' }, '*')
          }}
        />
      </div>

      {/* Cell 3 — Apply Filter (20%) */}
      <div style={{ flex: '0 0 20%', padding: '1em', borderTop: '0.125em solid #d8e2e1', boxSizing: 'border-box', position: 'relative' }}>
        <button
          type="button"
          data-testid="apply-filter-button"
          data-dirty={isDirty ? 'true' : 'false'}
          onClick={() => {
            if (!isDirty) {
              // Greyed click — show the spec-mandated non-modal popup.
              setPopupOpen(true)
              return
            }
            // Hot click — apply + clear change state (optimistic).
            window.parent.postMessage({ type: 'gui:filter-apply-click' }, '*')
            setAppliedCodes(currentCodes)
            setPopupOpen(false)
          }}
          style={{
            width: '100%', padding: '1em', borderRadius: 4,
            border: isDirty ? 'none' : '0.125em solid #cbd5d5',
            background: isDirty
              ? 'linear-gradient(180deg, #0b9a94, #0b7a75)'
              : '#e5e7eb',
            color: isDirty ? '#fff' : '#6b7280',
            fontSize: '1em', fontWeight: 600,
            cursor: isDirty ? 'pointer' : 'not-allowed',
            minHeight: 44,
          }}
        >
          Apply Filter
        </button>

        {/* Non-modal popup — EPIC-006-F-002-S-004-REQ-T-003 */}
        {popupOpen && (
          <div
            data-testid="apply-filter-no-changes-popup"
            role="dialog"
            aria-modal="false"
            style={{
              position: 'absolute', bottom: '100%', left: 8, right: 8,
              marginBottom: '1em', padding: '1em', borderRadius: 6,
              background: '#fff', border: '0.125em solid #0b7a75',
              boxShadow: '0 0.25em 1em rgba(0,0,0,0.15)',
              fontFamily: 'system-ui, sans-serif', fontSize: '1em',
              color: '#1f2937', zIndex: 10,
            }}
          >
            <div style={{ marginBottom: '1em', lineHeight: 1.4 }}>
              {NO_CHANGES_POPUP_TEXT}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                data-testid="apply-filter-no-changes-popup-ok"
                onClick={() => setPopupOpen(false)}
                style={{
                  padding: '1em', borderRadius: 4, border: 'none',
                  background: '#0b7a75', color: '#fff', fontSize: '1em',
                  fontWeight: 600, cursor: 'pointer',
                }}
              >OK</button>
            </div>
          </div>
        )}
      </div>

      {/* Cell 4 — session-verification placeholder (22%) */}
      <div
        id="guiSessionCell"
        style={{ flex: '0 0 22%', padding: '1em', borderTop: '0.125em solid #e5e7eb', boxSizing: 'border-box' }}
      />
    </div>
  )
}
