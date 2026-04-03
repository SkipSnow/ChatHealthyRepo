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

// Ref for filter apply callback — set by ChatWindow
const filterApplyCallbackRef = { current: null as ((codes: string[], params: any) => void) | null }

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
        case 'filter-apply':
          // User applied filter — store selected codes for ChatWindow to pick up
          if (filterApplyCallbackRef.current) {
            const codes = JSON.parse(msg.value || '[]')
            const params = JSON.parse(msg.searchParams || '{}')
            filterApplyCallbackRef.current(codes, params)
          }
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

  const showFilterPanel = useCallback((options: { code: string; name: string; classification: string }[],
                                       searchParams: any) => {
    // Send filter options to parent's left panel
    const html = renderFilterHTML(options)
    sendToParent('gui:filter', { html, searchParams: JSON.stringify(searchParams) })
  }, [])

  const hideFilterPanel = useCallback(() => {
    // Clear filter content but keep panel at 20% — user controls layout (future feature)
    sendToParent('gui:filter-clear')
  }, [])

  return {
    pagination,
    showPagination,
    hidePagination,
    getAfterNpi,
    getBeforeNpi,
    updateFromResponse,
    showFilterPanel,
    hideFilterPanel,
    onFilterApply: (cb: (codes: string[], params: any) => void) => {
      filterApplyCallbackRef.current = cb
    },
  }
}

function renderFilterHTML(options: { code: string; name: string; classification: string }[]): string {
  if (!options.length) return ''

  const itemStyle = `padding:6px 8px;display:flex;align-items:center;gap:8px;font-size:12px;
    border-bottom:1px solid #f0f0f0;cursor:pointer;`.replace(/\n\s+/g, '')

  const items = options.map(opt =>
    `<label style="${itemStyle}" title="${opt.code}">
      <input type="checkbox" data-gui-action="filter-toggle" data-gui-value="${opt.code}" checked
        style="accent-color:#0b7a75;width:14px;height:14px;" />
      <span style="color:#374151;">${opt.name}</span>
    </label>`
  ).join('')

  const totalItems = options.length
  const toggleScript = `
    (function(btn) {
      var panel = btn.closest('[data-filter-panel]');
      var boxes = panel.querySelectorAll('input[data-gui-action="filter-toggle"]');
      var checked = panel.querySelectorAll('input[data-gui-action="filter-toggle"]:checked').length;
      var majority = checked > boxes.length / 2;
      var newState = !majority;
      boxes.forEach(function(cb) { cb.checked = newState; });
      btn.textContent = newState ? 'Uncheck All' : 'Check All';
    })(this)
  `.replace(/\n\s+/g, ' ')

  // All start checked → majority checked → label "Uncheck All"
  const toggleLabel = 'Uncheck All'
  const toggleStyle = `padding:6px 8px;display:flex;align-items:center;gap:8px;font-size:11px;
    border-bottom:2px solid #d8e2e1;cursor:pointer;color:#0b7a75;font-weight:600;`.replace(/\n\s+/g, '')

  return `
    <div data-filter-panel style="display:flex;flex-direction:column;height:100%;font-family:system-ui,sans-serif;">
      <div style="padding:8px 10px;font-size:11px;font-weight:600;color:#0b7a75;
        border-bottom:1px solid #d8e2e1;text-transform:uppercase;letter-spacing:0.05em;">
        Filter by Specialty
      </div>
      <div style="${toggleStyle}">
        <button data-gui-action="toggle-all" onclick="${toggleScript}"
          style="background:none;border:1px solid #0b7a75;border-radius:3px;padding:3px 10px;
          font-size:11px;color:#0b7a75;cursor:pointer;font-weight:600;">${toggleLabel}</button>
      </div>
      <div style="flex:1;overflow-y:auto;overflow-x:hidden;">
        ${items}
      </div>
      <div style="padding:6px 8px;border-top:1px solid #d8e2e1;">
        <button data-gui-action="filter-apply" style="width:100%;padding:6px;border-radius:4px;
          border:none;background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;
          font-size:12px;font-weight:600;cursor:pointer;
          border-bottom:2px solid #065a56;box-shadow:0 1px 3px rgba(0,0,0,0.2);"
          onmousedown="this.style.transform='translateY(1px)';this.style.boxShadow='none'"
          onmouseup="this.style.transform='';this.style.boxShadow='0 1px 3px rgba(0,0,0,0.2)'"
          onmouseleave="this.style.transform='';this.style.boxShadow='0 1px 3px rgba(0,0,0,0.2)'"
        >Apply Filter</button>
      </div>
    </div>
  `
}
