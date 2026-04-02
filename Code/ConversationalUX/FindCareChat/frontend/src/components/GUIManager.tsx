// Copyright (c) 2026 Skip Snow. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// GUIManager — owns all non-chat GUI controls below the chat window.
// First widget: PaginationBar for multi-page result sets.
// Reusable: Evaluate Care, clinical trials, comparison results.

import { useState } from 'react'

export interface PaginationState {
  visible: boolean
  totalCount: number
  currentPage: number
  pageSize: number
  lastNpi: string         // keyset: last NPI on current page
  pageNpis: string[]      // stack of first NPIs per visited page (for back)
  searchParams: any       // original search params to re-call with after_npi
}

interface PaginationBarProps {
  state: PaginationState
  onPageForward: () => void
  onPageBack: () => void
  onPageSizeChange: (size: number) => void
  onClose: () => void
}

export function PaginationBar({ state, onPageForward, onPageBack, onPageSizeChange, onClose }: PaginationBarProps) {
  if (!state.visible) return null

  const totalPages = Math.ceil(state.totalCount / state.pageSize)
  const canGoBack = state.currentPage > 1
  const canGoForward = state.currentPage < totalPages

  const barStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 16px',
    borderTop: '1px solid #d1d5db',
    background: '#f9fafb',
    fontSize: 13,
    gap: 12,
  }

  const btnStyle = (enabled: boolean): React.CSSProperties => ({
    padding: '4px 12px',
    borderRadius: 4,
    border: '1px solid #d1d5db',
    background: enabled ? '#fff' : '#f3f4f6',
    color: enabled ? '#111' : '#9ca3af',
    cursor: enabled ? 'pointer' : 'not-allowed',
    fontSize: 13,
    fontWeight: 500,
  })

  return (
    <div style={barStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button
          onClick={onPageBack}
          disabled={!canGoBack}
          style={btnStyle(canGoBack)}
        >
          ← Back
        </button>

        <span style={{ color: '#6b7280', fontWeight: 500 }}>
          Page {state.currentPage} of {totalPages} ({state.totalCount.toLocaleString()} results)
        </span>

        <button
          onClick={onPageForward}
          disabled={!canGoForward}
          style={btnStyle(canGoForward)}
        >
          Forward →
        </button>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ color: '#6b7280' }}>Per page:</label>
        <select
          value={state.pageSize}
          onChange={e => onPageSizeChange(Number(e.target.value))}
          style={{
            padding: '3px 8px',
            borderRadius: 4,
            border: '1px solid #d1d5db',
            fontSize: 13,
            background: '#fff',
          }}
        >
          {[5, 10, 25, 50, 100].map(n => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>

        <button
          onClick={onClose}
          style={{
            padding: '3px 10px',
            borderRadius: 4,
            border: '1px solid #d1d5db',
            background: '#fff',
            color: '#6b7280',
            cursor: 'pointer',
            fontSize: 12,
          }}
          title="Close pagination"
        >
          ✕
        </button>
      </div>
    </div>
  )
}

export function useGUIManager() {
  const [pagination, setPagination] = useState<PaginationState>({
    visible: false,
    totalCount: 0,
    currentPage: 1,
    pageSize: 25,
    lastNpi: '',
    pageNpis: [''],  // page 1 starts with no after_npi
    searchParams: null,
  })

  const showPagination = (totalCount: number, lastNpi: string, searchParams: any, pageSize?: number) => {
    setPagination({
      visible: true,
      totalCount,
      currentPage: 1,
      pageSize: pageSize ?? 25,
      lastNpi,
      pageNpis: [''],
      searchParams,
    })
  }

  const hidePagination = () => {
    setPagination(prev => ({ ...prev, visible: false }))
  }

  const setPageSize = (size: number) => {
    // Reset to page 1 with new page size
    setPagination(prev => ({
      ...prev,
      pageSize: size,
      currentPage: 1,
      lastNpi: '',
      pageNpis: [''],
    }))
  }

  const pageForward = () => {
    setPagination(prev => {
      const newPage = prev.currentPage + 1
      const newPageNpis = [...prev.pageNpis]
      if (newPageNpis.length < newPage) {
        newPageNpis.push(prev.lastNpi)
      }
      return { ...prev, currentPage: newPage, pageNpis: newPageNpis }
    })
  }

  const pageBack = () => {
    setPagination(prev => ({
      ...prev,
      currentPage: Math.max(1, prev.currentPage - 1),
    }))
  }

  const getAfterNpi = (): string => {
    // For current page, return the after_npi to use
    const idx = pagination.currentPage - 1
    return pagination.pageNpis[idx] ?? ''
  }

  const updateLastNpi = (npi: string) => {
    setPagination(prev => ({ ...prev, lastNpi: npi }))
  }

  return {
    pagination,
    showPagination,
    hidePagination,
    setPageSize,
    pageForward,
    pageBack,
    getAfterNpi,
    updateLastNpi,
  }
}
