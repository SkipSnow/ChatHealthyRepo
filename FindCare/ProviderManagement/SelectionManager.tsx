// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SelectionManager — split view for provider selection.
// FC-SELECT-001: Available providers (top) + Selected providers (bottom, max 5).
// EPIC-9 UX Management: reusable component.
//
// Features:
//   - Drag and drop from available to selected
//   - Arrow button to select
//   - Delete icon to deselect
//   - Garbage icon to dismiss (with count badge)
//   - Filtered-out selected providers highlighted with keep/remove dialog
//   - Selection persists across pagination and filter changes

import React, { useState } from 'react'
import type { Provider, FilterOption } from './provider'
import type { SelectionState } from './provider'
import { ProviderCard } from './ProviderCard'

interface SelectionManagerProps {
  state: SelectionState
  activeFilterCodes?: Set<string>
  onSelect: (npi: string) => void
  onDeselect: (npi: string) => void
  onDismiss: (npi: string) => void
  onKeepFiltered: (npi: string) => void
  onRemoveFiltered: (npi: string) => void
  isFull: boolean
}

export function SelectionManager({
  state,
  activeFilterCodes,
  onSelect,
  onDeselect,
  onDismiss,
  onKeepFiltered,
  onRemoveFiltered,
  isFull,
}: SelectionManagerProps) {
  const [dragOver, setDragOver] = useState(false)
  const [filteredDialog, setFilteredDialog] = useState<string | null>(null)

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const npi = e.dataTransfer.getData('text/plain')
    if (npi) onSelect(npi)
  }

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(true)
  }

  const handleDragLeave = () => setDragOver(false)

  const handleFilteredClick = (npi: string) => {
    setFilteredDialog(npi)
  }

  const isProviderFilteredOut = (provider: Provider): boolean => {
    if (!activeFilterCodes || activeFilterCodes.size === 0) return false
    const specCode = provider.taxonomy_code || provider.specialty || ''
    return specCode !== '' && !activeFilterCodes.has(specCode)
  }

  // Kept NPIs — user chose to keep despite filter
  const [keptNpis] = useState<Set<string>>(new Set())

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', fontFamily: 'system-ui, sans-serif',
      background: '#fff', border: '0.125em solid #d8e2e1', borderRadius: '0.75em', overflow: 'hidden',
    }}>
      {/* Available providers — top */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '1em', background: '#f0fffe', borderBottom: '0.125em solid #d8e2e1',
      }}>
        <span style={{ fontSize: '1em', fontWeight: 700, color: '#0b7a75', textTransform: 'uppercase' }}>
          Available Providers
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1em' }}>
          <span style={{ fontSize: '1em', color: '#6b7280' }}>{state.available.length} available</span>
          <span title={state.garbage.length > 0 ? `${state.garbage.length} dismissed — new question restores them` : 'No dismissed providers'} style={{
            fontSize: '1em', color: state.garbage.length > 0 ? '#dc2626' : '#d1d5db',
            cursor: 'default', position: 'relative',
          }}>
            🗑️ <span style={{
              fontSize: '1em', fontWeight: 700,
              color: state.garbage.length > 0 ? '#dc2626' : '#9ca3af',
            }}>{state.garbage.length}</span>
          </span>
        </div>
      </div>

      <div style={{ maxHeight: '31.25em', overflowY: 'auto', overflowX: 'hidden' }}>
        {state.available.length === 0 ? (
          <div style={{ padding: '1em', textAlign: 'center', color: '#9ca3af', fontSize: '1em' }}>
            No providers available
          </div>
        ) : (
          state.available.map(p => (
            <ProviderCard
              key={p.npi}
              provider={p}
              mode="available"
              onSelect={onSelect}
              onDismiss={onDismiss}
              selectionFull={isFull}
            />
          ))
        )}
      </div>

      {/* Drop zone divider */}
      <div
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        style={{
          padding: '1em',
          background: dragOver ? '#fef3c7' : '#fffbeb',
          borderTop: '0.125em solid #d8e2e1',
          borderBottom: '0.125em solid #d8e2e1',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          transition: 'background 0.15s',
          border: dragOver ? '0.25em dashed #d97706' : undefined,
        }}
      >
        <span style={{ fontSize: '1em', fontWeight: 600, color: '#d97706', textTransform: 'uppercase' }}>
          Selected for Evaluation
        </span>
        <span style={{ fontSize: '1em', color: '#6b7280' }}>
          {state.selected.length} / {state.maxSelected}
        </span>
      </div>

      {/* Selected providers — bottom */}
      <div
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        style={{
          minHeight: '5em', maxHeight: '16.25em', overflowY: 'auto', overflowX: 'hidden',
          background: dragOver ? '#fffdf0' : '#fffdf7',
        }}
      >
        {state.selected.length === 0 ? (
          <div style={{ padding: '1em', textAlign: 'center', color: '#9ca3af', fontSize: '1em' }}>
            Drag providers here or click ↓ to select (max {state.maxSelected})
          </div>
        ) : (
          state.selected.map(p => {
            const filteredOut = isProviderFilteredOut(p) && !keptNpis.has(p.npi)
            return (
              <React.Fragment key={p.npi}>
                <ProviderCard
                  provider={p}
                  mode="selected"
                  compact={true}
                  isFilteredOut={filteredOut}
                  onDeselect={onDeselect}
                  onFilteredClick={handleFilteredClick}
                  draggable={false}
                />
                {filteredDialog === p.npi && (
                  <div style={{
                    padding: '1em', background: '#fef3c7', borderBottom: '0.125em solid #f59e0b',
                    display: 'flex', gap: '1em', alignItems: 'center', fontSize: '1em',
                  }}>
                    <span style={{ flex: 1, color: '#92400e' }}>Not in your filter — keep this provider?</span>
                    <button
                      style={{
                        padding: '1em', fontSize: '1em', fontWeight: 600,
                        background: '#0b7a75', color: '#fff', border: 'none', borderRadius: '0.375em', cursor: 'pointer',
                      }}
                      onClick={() => {
                        keptNpis.add(p.npi)
                        onKeepFiltered(p.npi)
                        setFilteredDialog(null)
                      }}
                    >Keep</button>
                    <button
                      style={{
                        padding: '1em', fontSize: '1em', fontWeight: 600,
                        background: '#dc2626', color: '#fff', border: 'none', borderRadius: '0.375em', cursor: 'pointer',
                      }}
                      onClick={() => {
                        onRemoveFiltered(p.npi)
                        setFilteredDialog(null)
                      }}
                    >Remove</button>
                  </div>
                )}
              </React.Fragment>
            )
          })
        )}
      </div>
    </div>
  )
}
