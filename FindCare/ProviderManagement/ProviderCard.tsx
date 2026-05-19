// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ProviderCard — single provider display with action buttons.
// EPIC-9 UX Management: reusable component.
// Used by FindCare (search results) and EvaluateCare (scored results).

import React from 'react'
import type { Provider } from './provider'

interface ProviderCardProps {
  provider: Provider
  mode: 'available' | 'selected'
  compact?: boolean  // EPIC-006-F-001-S-002-REQ-B-015: selected list shows name, specialty, NPI only
  isFilteredOut?: boolean
  onSelect?: (npi: string) => void
  onDeselect?: (npi: string) => void
  onDismiss?: (npi: string) => void
  onFilteredClick?: (npi: string) => void
  selectionFull?: boolean
  draggable?: boolean
}

const cardStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  padding: '1em',
  borderBottom: '0.125em solid #f0f0f0',
  fontSize: '1em',
  lineHeight: 1.3,
  cursor: 'default',
  transition: 'background 0.15s',
}

const filteredOutStyle: React.CSSProperties = {
  ...cardStyle,
  background: '#fff7ed',
  borderLeft: '0.375em solid #f59e0b',
}

const nameStyle: React.CSSProperties = {
  fontWeight: 600,
  color: '#1f2937',
  fontSize: '1em',
}

const detailStyle: React.CSSProperties = {
  fontSize: '1em',
  color: '#6b7280',
}

const actionBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: '1em',
  padding: '1em',
  flexShrink: 0,
}

export function ProviderCard({
  provider,
  mode,
  compact = false,
  isFilteredOut = false,
  onSelect,
  onDeselect,
  onDismiss,
  onFilteredClick,
  selectionFull = false,
  draggable = true,
}: ProviderCardProps) {
  const style = isFilteredOut ? filteredOutStyle : cardStyle
  const p = provider
  const tooltipText = compact
    ? `${p.address || ''}\n${p.county || ''}\nPhone: ${p.phone || 'N/A'}`
    : (isFilteredOut ? 'Not in your filter — click to keep or remove' : '')

  const handleDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData('text/plain', p.npi)
    e.dataTransfer.effectAllowed = 'move'
  }

  return (
    <div
      style={style}
      draggable={draggable && mode === 'available'}
      onDragStart={handleDragStart}
      title={tooltipText}
      onClick={isFilteredOut && onFilteredClick ? () => onFilteredClick(p.npi) : undefined}
    >
      {/* Provider info */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={nameStyle}>{p.name}</div>
        {(p.specialty || p.primary_specialty) && (
          <div style={{ fontSize: '1em', color: '#0b7a75', fontWeight: 500 }}>
            {p.specialty || p.primary_specialty}
          </div>
        )}
        {!compact && p.address && <div style={detailStyle}>{p.address}</div>}
        {!compact && p.county && <div style={detailStyle}>{p.county}</div>}
        {!compact && p.phone && <div style={detailStyle}>Phone: {p.phone}</div>}
        <div style={{ ...detailStyle, color: '#9ca3af' }}>NPI: {p.npi}</div>
      </div>

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: '0.25em', alignItems: 'center' }}>
        {mode === 'available' && (
          <>
            <button
              style={{ ...actionBtnStyle, color: selectionFull ? '#d1d5db' : '#0b7a75' }}
              title={selectionFull ? 'Selection full (max 5)' : 'Select for evaluation'}
              onClick={(e) => { e.stopPropagation(); onSelect?.(p.npi) }}
              disabled={selectionFull}
            >↓</button>
            <button
              style={{ ...actionBtnStyle, color: '#9ca3af' }}
              title="Dismiss"
              onClick={(e) => { e.stopPropagation(); onDismiss?.(p.npi) }}
            >🗑</button>
          </>
        )}
        {mode === 'selected' && !isFilteredOut && (
          <button
            style={{ ...actionBtnStyle, color: '#dc2626' }}
            title="Remove from selection"
            onClick={(e) => { e.stopPropagation(); onDeselect?.(p.npi) }}
          >✕</button>
        )}
      </div>
    </div>
  )
}
