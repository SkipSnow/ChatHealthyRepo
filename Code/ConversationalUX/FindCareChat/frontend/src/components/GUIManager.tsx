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

function renderEvaluateButtonHTML(): string {
  return `
    <button data-gui-action="evaluate-providers"
      style="padding:8px 20px;border-radius:5px;font-size:13px;font-weight:600;
        font-family:system-ui,sans-serif;cursor:pointer;color:#fff;
        background:linear-gradient(180deg,#0b9a94,#0b7a75);border:none;
        border-bottom:3px solid #065a56;
        box-shadow:0 2px 4px rgba(0,0,0,0.25),inset 0 1px 0 rgba(255,255,255,0.15);
        transition:all 0.08s ease;user-select:none;margin-left:16px;"
      onmousedown="this.style.transform='translateY(2px)';this.style.boxShadow='inset 0 2px 4px rgba(0,0,0,0.2)'"
      onmouseup="this.style.transform='';this.style.boxShadow='0 2px 4px rgba(0,0,0,0.25),inset 0 1px 0 rgba(255,255,255,0.15)'"
      onmouseleave="this.style.transform='';this.style.boxShadow='0 2px 4px rgba(0,0,0,0.25),inset 0 1px 0 rgba(255,255,255,0.15)'"
      title="Send these providers to EvaluateCare for quality evaluation"
    >Evaluate These Providers</button>
  `
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
      ${renderEvaluateButtonHTML()}
    </div>
  `
}

// ── Hook ────────────────────────────────────────────────────

// Ref for filter apply callback — set by ChatWindow
const filterApplyCallbackRef = { current: null as ((codes: string[], params: any) => void) | null }
// Ref for evaluate providers callback — set by ChatWindow
const evaluateCallbackRef = { current: null as (() => void) | null }
// Ref for stop thinking callback — set by ChatWindow
const stopThinkingCallbackRef = { current: null as (() => void) | null }

// Cache full specialty list for client-side filter toggles (no server round-trip)
const cachedFilterOptions = { current: [] as any[] }
const cachedSearchParams = { current: null as any }
const cachedFilterState = { prescribers: true, homeopathic: false }

// Filter helpers — use can_prescribe/homeopathic flags from DB when available,
// fall back to taxonomy prefix check if flags missing (pre-migration data)
const PRESCRIBER_TAX_PREFIXES = ['207', '208', '209', '204', '174400000X', '363L', '363A', '367', '122', '213', '176']

function isPrescriberOption(opt: any): boolean {
  if ('can_prescribe' in opt) return opt.can_prescribe === true
  return PRESCRIBER_TAX_PREFIXES.some(p => opt.code?.startsWith(p))
}

function isHomeopathicOption(opt: any): boolean {
  if ('homeopathic' in opt) return opt.homeopathic === true
  return false
}

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
        case 'evaluate-providers':
          if (evaluateCallbackRef.current) {
            evaluateCallbackRef.current()
          }
          break
        case 'filter-apply':
          // User applied filter — store selected codes for ChatWindow to pick up
          if (filterApplyCallbackRef.current) {
            const codes = JSON.parse(msg.value || '[]')
            const params = JSON.parse(msg.searchParams || '{}')
            if (msg.prescribers_only) {
              params.prescribers_only = true
            }
            filterApplyCallbackRef.current(codes, params)
          }
          break
        case 'stop-thinking':
          // Stop button in control frame — dismiss thinking state
          if (stopThinkingCallbackRef.current) {
            stopThinkingCallbackRef.current()
          }
          break
        case 'prescribers-only-toggle':
        case 'homeopathic-only-toggle':
          // Client-side filter — no server round-trip
          if (cachedFilterOptions.current.length > 0) {
            // Read current filter state from the message
            const isPrescribers = msg.action === 'prescribers-only-toggle'
              ? msg.value === 'true'
              : cachedFilterState.prescribers
            const isHomeopathic = msg.action === 'homeopathic-only-toggle'
              ? msg.value === 'true'
              : cachedFilterState.homeopathic

            cachedFilterState.prescribers = isPrescribers
            cachedFilterState.homeopathic = isHomeopathic

            let options = cachedFilterOptions.current
            if (isPrescribers) options = options.filter(opt => isPrescriberOption(opt))
            if (isHomeopathic) options = options.filter(opt => isHomeopathicOption(opt))

            const html = renderFilterHTML(
              options.length > 0 ? options : cachedFilterOptions.current,
              0,
              isPrescribers,
              isHomeopathic,
            )
            sendToParent('gui:filter', {
              html,
              searchParams: JSON.stringify(cachedSearchParams.current || {}),
            })
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

  const showFilterPanel = useCallback((options: any[],
                                       searchParams: any,
                                       totalProviders: number = 0) => {
    // Cache full list for client-side filter toggles
    cachedFilterOptions.current = options
    cachedSearchParams.current = searchParams
    cachedFilterState.prescribers = true
    cachedFilterState.homeopathic = false
    const html = renderFilterHTML(options, totalProviders, true, false)
    sendToParent('gui:filter', { html, searchParams: JSON.stringify(searchParams), applyInitialFilter: true })
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
    onEvaluateProviders: (cb: () => void) => {
      evaluateCallbackRef.current = cb
    },
    onStopThinking: (cb: () => void) => {
      stopThinkingCallbackRef.current = cb
    },
  }
}

function renderFilterHTML(options: any[], totalProviders: number = 0, prescribersChecked: boolean = true, homeopathicChecked: boolean = false): string {
  if (!options.length) return ''

  const allCount = options.length
  const prescCount = options.filter(o => o.can_prescribe).length
  const homeoCount = options.filter(o => o.homeopathic).length
  // Filtered count: prescribers + homeopathic overlap
  const filteredCount = prescribersChecked || homeopathicChecked
    ? options.filter(o => (!prescribersChecked || o.can_prescribe) && (!homeopathicChecked || o.homeopathic)).length
    : allCount
  const checkedCount = filteredCount  // Initially all visible are checked

  const items = options.map(opt =>
    `<div style="display:block;padding:5px 10px;border-bottom:1px solid #eee;" data-spec-code="${opt.code}" data-can-prescribe="${opt.can_prescribe || false}" data-homeopathic="${opt.homeopathic || false}">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px;line-height:1.3;">
        <input type="checkbox" data-gui-action="filter-toggle" data-gui-value="${opt.code}" checked
          style="accent-color:#0b7a75;width:14px;height:14px;flex-shrink:0;" />
        <span style="color:#1f2937;">${opt.name}</span>
      </label>
    </div>`
  ).join('')

  // Mockup: horizontal grid header with labeled counts + checkboxes on right
  // [Filter by specialty] [All possible: N] [Prescribers & homeopathic: N] [Your choices: N] [checkboxes]
  const presHomeoLabel = homeopathicChecked ? 'Prescribers &amp; homeopathic' : 'Prescribers'

  return `
    <div data-filter-panel style="display:flex;flex-direction:column;font-family:system-ui,sans-serif;background:#fff;">
      <div style="padding:8px 10px;border-bottom:2px solid #0b7a75;background:#f8fffe;">
        <div style="display:flex;align-items:center;gap:0;flex-wrap:nowrap;">
          <div style="flex:0 0 auto;padding-right:10px;border-right:1px solid #d8e2e1;">
            <div style="font-size:11px;font-weight:700;color:#0b7a75;text-transform:uppercase;letter-spacing:0.03em;white-space:nowrap;">Filter by specialty</div>
          </div>
          <div style="flex:0 0 auto;padding:0 10px;border-right:1px solid #d8e2e1;text-align:center;">
            <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">All possible</div>
            <div style="font-size:14px;font-weight:700;color:#1f2937;" id="filterAllCount">${allCount}</div>
          </div>
          <div style="flex:0 0 auto;padding:0 10px;border-right:1px solid #d8e2e1;text-align:center;">
            <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">${presHomeoLabel}</div>
            <div style="font-size:14px;font-weight:700;color:#1f2937;" id="filterFilteredCount">${filteredCount}</div>
          </div>
          <div style="flex:0 0 auto;padding:0 10px;border-right:1px solid #d8e2e1;text-align:center;">
            <div style="font-size:8px;color:#6b7280;text-transform:uppercase;">Your choices</div>
            <div style="font-size:14px;font-weight:700;color:#0b7a75;" id="filterShowing">${checkedCount}</div>
          </div>
          <div style="flex:0 0 auto;padding-left:10px;display:flex;flex-direction:column;gap:2px;justify-content:center;">
            <label style="font-size:9px;color:#1f2937;display:flex;align-items:center;gap:3px;cursor:pointer;white-space:nowrap;">
              <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="prescribers" ${prescribersChecked ? 'checked' : ''}
                style="accent-color:#0b7a75;width:12px;height:12px;" />
              Prescribers
            </label>
            <label style="font-size:9px;color:#1f2937;display:flex;align-items:center;gap:3px;cursor:pointer;white-space:nowrap;">
              <input type="checkbox" data-gui-action="filter-provider-type" data-gui-value="homeopathic" ${homeopathicChecked ? 'checked' : ''}
                style="accent-color:#0b7a75;width:12px;height:12px;" />
              Homeopathic
            </label>
          </div>
        </div>
      </div>
      <div style="padding:4px 10px;display:flex;align-items:center;border-bottom:1px solid #e5e7eb;background:#fafafa;">
        <button data-gui-action="toggle-all"
          style="background:none;border:1px solid #0b7a75;border-radius:3px;padding:2px 10px;
          font-size:10px;color:#0b7a75;cursor:pointer;font-weight:600;">Uncheck All</button>
      </div>
      <div style="max-height:390px;overflow-y:auto;overflow-x:hidden;">
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
        <button data-gui-action="evaluate-providers" style="width:100%;padding:6px;border-radius:4px;
          border:none;background:linear-gradient(180deg,#d97706,#b45309);color:#fff;
          font-size:12px;font-weight:600;cursor:pointer;margin-top:6px;
          border-bottom:2px solid #92400e;box-shadow:0 1px 3px rgba(0,0,0,0.2);"
          onmousedown="this.style.transform='translateY(1px)';this.style.boxShadow='none'"
          onmouseup="this.style.transform='';this.style.boxShadow='0 1px 3px rgba(0,0,0,0.2)'"
          onmouseleave="this.style.transform='';this.style.boxShadow='0 1px 3px rgba(0,0,0,0.2)'"
        >Evaluate These Providers</button>
      </div>
    </div>
  `
}
