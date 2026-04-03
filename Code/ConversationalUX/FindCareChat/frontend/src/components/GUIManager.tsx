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
  pageStart: number
  pageEnd: number
  pageSize: number
  firstNpi: string
  lastNpi: string
  npiHistory: string[]  // stack of after_npi values for back navigation
  searchParams: any
}

// ── PostMessage Bridge ──────────────────────────────────────

function sendToParent(type: string, payload: any = {}) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type, ...payload }, '*')
  }
}

function renderPaginationHTML(state: PaginationState): string {
  const canBack = state.pageStart > 1
  const canForward = state.pageEnd < state.totalCount

  // Cold gray (inactive/up) and warm gray (pressed/down)
  const coldGray = '#6b7280'   // blue-tinted gray
  const warmGray = '#78716c'   // warm stone gray
  const disabledGray = '#d1d5db'

  const btn3d = (enabled: boolean) => `
    padding:8px 20px;
    border-radius:5px;
    font-size:14px;
    font-weight:600;
    font-family:system-ui,sans-serif;
    cursor:${enabled ? 'pointer' : 'not-allowed'};
    color:#fff;
    background:${enabled ? `linear-gradient(180deg, #8b8f96 0%, ${coldGray} 100%)` : disabledGray};
    border:none;
    border-bottom:${enabled ? `3px solid #4b5563` : `2px solid #c0c0c0`};
    box-shadow:${enabled ? '0 2px 4px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.15)' : 'none'};
    transition:all 0.08s ease;
    user-select:none;
  `.replace(/\n\s+/g, '')

  // Mouse down: warm gray, depressed
  const pressDown = `this.style.background='linear-gradient(180deg, ${warmGray} 0%, #57534e 100%)';this.style.borderBottom='1px solid #44403c';this.style.transform='translateY(2px)';this.style.boxShadow='inset 0 2px 4px rgba(0,0,0,0.2)';`
  // Mouse up: back to cold gray, raised
  const pressUp = `this.style.background='linear-gradient(180deg, #8b8f96 0%, ${coldGray} 100%)';this.style.borderBottom='3px solid #4b5563';this.style.transform='translateY(0)';this.style.boxShadow='0 2px 4px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.15)';`

  return `
    <div style="display:flex;align-items:center;justify-content:center;gap:20px;padding:8px 16px;font-size:13px;font-family:system-ui,sans-serif;height:100%;">
      <button data-gui-action="page-back" style="${btn3d(canBack)}"
        title="${canBack ? 'Show previous records' : 'You are at the beginning'}"
        ${canBack ? `onmousedown="${pressDown}" onmouseup="${pressUp}" onmouseleave="${pressUp}"` : 'disabled'}
      >&laquo; Back</button>
      <span style="color:#374151;font-weight:600;font-size:14px;min-width:160px;text-align:center;letter-spacing:0.02em;">
        ${state.pageStart.toLocaleString()}\u2013${state.pageEnd.toLocaleString()} / ${state.totalCount.toLocaleString()}
      </span>
      <button data-gui-action="page-forward" style="${btn3d(canForward)}"
        title="${canForward ? 'Show next records' : 'You are at the end'}"
        ${canForward ? `onmousedown="${pressDown}" onmouseup="${pressUp}" onmouseleave="${pressUp}"` : 'disabled'}
      >Forward &raquo;</button>
    </div>
  `
}

// ── Hook ────────────────────────────────────────────────────

export function useGUIManager() {
  const [pagination, setPagination] = useState<PaginationState>({
    visible: false,
    totalCount: 0,
    pageStart: 1,
    pageEnd: 0,
    pageSize: 25,
    firstNpi: '',
    lastNpi: '',
    npiHistory: [],
    searchParams: null,
  })

  // Listen for events from the static frame
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const msg = event.data
      if (!msg || typeof msg !== 'object' || msg.type !== 'gui:event') return

      switch (msg.action) {
        case 'page-forward':
          setPagination(prev => ({
            ...prev,
            npiHistory: [...prev.npiHistory, prev.firstNpi],
            direction: 'forward',
          } as any))
          break
        case 'page-back':
          setPagination(prev => {
            const history = [...prev.npiHistory]
            history.pop()  // remove current
            return { ...prev, npiHistory: history, direction: 'back' } as any
          })
          break
      }
    }
    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [])

  // Push render to parent whenever pagination state changes
  useEffect(() => {
    if (pagination.visible) {
      console.log('[GUIManager] Sending gui:render to parent', pagination)
      sendToParent('gui:render', { html: renderPaginationHTML(pagination) })
    }
  }, [pagination])

  const showPagination = useCallback((totalCount: number, pageStart: number, pageEnd: number,
                                       firstNpi: string, lastNpi: string, searchParams: any,
                                       pageSize: number) => {
    setPagination(prev => ({
      visible: true,
      totalCount,
      pageStart,
      pageEnd,
      pageSize,
      firstNpi,
      lastNpi,
      npiHistory: prev.npiHistory,
      searchParams,
    }))
  }, [])

  const hidePagination = useCallback(() => {
    setPagination(prev => ({ ...prev, visible: false, npiHistory: [] }))
    sendToParent('gui:clear')
  }, [])

  const getAfterNpi = useCallback((): string => {
    return pagination.lastNpi
  }, [pagination])

  const getBeforeNpi = useCallback((): string => {
    const history = pagination.npiHistory
    return history.length > 0 ? history[history.length - 1] : ''
  }, [pagination])

  const updateFromResponse = useCallback((data: any) => {
    if (data.total_count > 0) {
      setPagination(prev => ({
        ...prev,
        visible: true,
        totalCount: data.total_count,
        pageStart: data.page_start,
        pageEnd: data.page_end,
        firstNpi: data.first_npi || '',
        lastNpi: data.last_npi || '',
      }))
    }
  }, [])

  return {
    pagination,
    showPagination,
    hidePagination,
    getAfterNpi,
    getBeforeNpi,
    updateFromResponse,
  }
}
