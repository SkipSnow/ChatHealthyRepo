// Copyright (c) 2026 Skip Snow. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// GUIManager — orchestrates GUI controls on the static page's control frame.
// React app sends HTML to render via postMessage. Static frame relays clicks back.
// The React app owns all state. The static page is a rendering surface.

import { useState, useEffect, useCallback } from 'react'

// Responsive max rows: PC=20, phone=5. Configurable.
const MAX_ROWS_PC = 20
const MAX_ROWS_MOBILE = 5

function getMaxRows(): number {
  if (typeof window === 'undefined') return MAX_ROWS_PC
  return window.innerWidth < 768 ? MAX_ROWS_MOBILE : MAX_ROWS_PC
}

export interface PaginationState {
  visible: boolean
  totalCount: number
  currentPage: number
  pageSize: number
  lastNpi: string
  pageNpis: string[]
  searchParams: any
}

// ── PostMessage Bridge ──────────────────────────────────────

function sendToParent(type: string, payload: any = {}) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type, ...payload }, '*')
  }
}

function renderPaginationHTML(state: PaginationState): string {
  const totalPages = Math.ceil(state.totalCount / state.pageSize)
  const canBack = state.currentPage > 1
  const canForward = state.currentPage < totalPages

  const btnStyle = (enabled: boolean) =>
    `padding:4px 12px;border-radius:4px;border:1px solid #d1d5db;` +
    `background:${enabled ? '#fff' : '#f3f4f6'};color:${enabled ? '#111' : '#9ca3af'};` +
    `cursor:${enabled ? 'pointer' : 'not-allowed'};font-size:13px;font-weight:500;`

  const maxRows = getMaxRows()
  const sizeOptions = [5, 10, 15, 20].filter(n => n <= maxRows)
  const options = sizeOptions.map(n =>
    `<option value="${n}" ${n === state.pageSize ? 'selected' : ''}>${n}</option>`
  ).join('')

  return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 16px;font-size:13px;font-family:system-ui,sans-serif;">
      <div style="display:flex;align-items:center;gap:8px;">
        <button data-gui-action="page-back" data-gui-value="" style="${btnStyle(canBack)}" ${canBack ? '' : 'disabled'}>← Back</button>
        <span style="color:#6b7280;font-weight:500;">Page ${state.currentPage} of ${totalPages} (${state.totalCount.toLocaleString()} results)</span>
        <button data-gui-action="page-forward" data-gui-value="" style="${btnStyle(canForward)}" ${canForward ? '' : 'disabled'}>Forward →</button>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <label style="color:#6b7280;">Per page:</label>
        <select data-gui-action="page-size" style="padding:3px 8px;border-radius:4px;border:1px solid #d1d5db;font-size:13px;background:#fff;">
          ${options}
        </select>
        <button data-gui-action="close" data-gui-value="" style="padding:3px 10px;border-radius:4px;border:1px solid #d1d5db;background:#fff;color:#6b7280;cursor:pointer;font-size:12px;" title="Close">✕</button>
      </div>
    </div>
  `
}

// ── Hook ────────────────────────────────────────────────────

export function useGUIManager() {
  const [pagination, setPagination] = useState<PaginationState>({
    visible: false,
    totalCount: 0,
    currentPage: 1,
    pageSize: 25,
    lastNpi: '',
    pageNpis: [''],
    searchParams: null,
  })

  // Listen for events from the static frame
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object' || msg.type !== 'gui:event') return

      switch (msg.action) {
        case 'page-forward':
          setPagination(prev => {
            const newPage = prev.currentPage + 1
            const newPageNpis = [...prev.pageNpis]
            if (newPageNpis.length < newPage) newPageNpis.push(prev.lastNpi)
            return { ...prev, currentPage: newPage, pageNpis: newPageNpis }
          })
          break
        case 'page-back':
          setPagination(prev => ({ ...prev, currentPage: Math.max(1, prev.currentPage - 1) }))
          break
        case 'page-size':
          const size = parseInt(msg.value) || 25
          setPagination(prev => ({ ...prev, pageSize: size, currentPage: 1, pageNpis: [''], lastNpi: '' }))
          break
        case 'close':
          setPagination(prev => ({ ...prev, visible: false }))
          sendToParent('gui:clear')
          break
      }
    }
    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [])

  // Push render to parent whenever pagination state changes
  useEffect(() => {
    if (pagination.visible) {
      sendToParent('gui:render', { html: renderPaginationHTML(pagination) })
    }
  }, [pagination])

  const showPagination = useCallback((totalCount: number, lastNpi: string, searchParams: any) => {
    setPagination({
      visible: true,
      totalCount,
      currentPage: 1,
      pageSize: Math.min(getMaxRows(), 10),  // sensible default within device max
      lastNpi,
      pageNpis: [''],
      searchParams,
    })
  }, [])

  const hidePagination = useCallback(() => {
    setPagination(prev => ({ ...prev, visible: false }))
    sendToParent('gui:clear')
  }, [])

  const getAfterNpi = useCallback((): string => {
    const idx = pagination.currentPage - 1
    return pagination.pageNpis[idx] ?? ''
  }, [pagination])

  const updateLastNpi = useCallback((npi: string) => {
    setPagination(prev => ({ ...prev, lastNpi: npi }))
  }, [])

  return {
    pagination,
    showPagination,
    hidePagination,
    getAfterNpi,
    updateLastNpi,
  }
}
