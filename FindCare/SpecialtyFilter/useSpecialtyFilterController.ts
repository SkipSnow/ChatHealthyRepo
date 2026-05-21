// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SpecialtyFilter controller hook — all data, state, computeds, handlers,
// and parent-iframe postMessage wiring. No JSX. Paired with SpecialtyFilter.tsx
// which renders this hook's output.
import { useState, useMemo, useEffect, useCallback } from 'react'

export interface SpecialtyRecord {
  code: string
  name: string
  can_prescribe: boolean
  homeopathic: boolean
  homeopathic_general?: boolean
  rank: number
}

const NO_CHANGES_POPUP_TEXT =
  'There are no changes to apply, try a new prompt if this is the wrong list. ' +
  'The AI agent will help you craft the right question'

function codesKey(codes: string[]): string {
  return [...codes].sort().join('|')
}

export interface SpecialtyFilterControllerView {
  specialties: SpecialtyRecord[]
  sortedRows: SpecialtyRecord[]
  checked: Record<string, boolean>
  allPossibleCount: number
  allPrescribersCount: number
  yourChoicesCount: number
  labelIsCheckAll: boolean
  uncheckAllDisabled: boolean
  prescribersChecked: boolean
  homeopathicChecked: boolean
  isDirty: boolean
  popupOpen: boolean
  noChangesText: string
  toggleRow: (code: string) => void
  handleToggleAll: () => void
  handlePrescribersToggle: () => void
  handleHomeopathicToggle: () => void
  handleApplyClick: () => void
  closePopup: () => void
}

export function useSpecialtyFilterController(): SpecialtyFilterControllerView {
  const [specialties, setSpecialties] = useState<SpecialtyRecord[]>([])
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const [appliedCodes, setAppliedCodes] = useState<string[] | null>(null)
  const [currentCodes, setCurrentCodes] = useState<string[]>([])
  const [popupOpen, setPopupOpen] = useState(false)

  useEffect(() => {
    function handler(e: MessageEvent) {
      const data: any = e.data
      if (!data || typeof data !== 'object') return
      if (data.type === 'gui:filter-data') {
        const incoming = (data.specialties || []) as SpecialtyRecord[]
        const initial: Record<string, boolean> = {}
        for (const s of incoming) initial[s.code] = !!s.can_prescribe
        const initialCodes = incoming
          .filter(s => !!s.can_prescribe)
          .map(s => s.code)
        setSpecialties(incoming)
        setChecked(initial)
        setAppliedCodes(initialCodes)
        setCurrentCodes(initialCodes)
        setPopupOpen(false)
      }
    }
    window.addEventListener('message', handler)
    window.parent.postMessage({ type: 'gui:filter-ready' }, '*')
    return () => window.removeEventListener('message', handler)
  }, [])

  const sortedRows = useMemo(() => {
    return [...specialties].sort((a, b) => {
      const aGen = a.homeopathic_general ? 1 : 0
      const bGen = b.homeopathic_general ? 1 : 0
      if (aGen !== bGen) return aGen - bGen
      return (a.rank ?? 0) - (b.rank ?? 0)
    })
  }, [specialties])

  const allPossibleCount = specialties.length
  const allPrescribersCount = useMemo(
    () => specialties.filter(s => s.can_prescribe).length,
    [specialties],
  )
  const yourChoicesCount = useMemo(
    () => Object.values(checked).filter(Boolean).length,
    [checked],
  )

  const total = sortedRows.length
  const labelIsCheckAll = total > 0 && yourChoicesCount * 2 < total
  const uncheckAllDisabled = !labelIsCheckAll && yourChoicesCount === 0

  const prescribersChecked = useMemo(() => {
    const pres = sortedRows.filter(s => s.can_prescribe)
    if (pres.length === 0) return false
    return pres.every(s => !!checked[s.code])
  }, [sortedRows, checked])
  const homeopathicChecked = useMemo(() => {
    const homeo = sortedRows.filter(s => s.homeopathic)
    if (homeo.length === 0) return false
    return homeo.every(s => !!checked[s.code])
  }, [sortedRows, checked])

  // Emit checked codes upward whenever they change.
  useEffect(() => {
    const codes: string[] = []
    for (const s of sortedRows) if (checked[s.code]) codes.push(s.code)
    setCurrentCodes(codes)
    window.parent.postMessage(
      { type: 'gui:filter-selection-change', codes },
      '*',
    )
  }, [checked, sortedRows])

  const isDirty = useMemo(() => {
    if (appliedCodes === null) return false
    return codesKey(currentCodes) !== codesKey(appliedCodes)
  }, [currentCodes, appliedCodes])

  const toggleRow = useCallback((code: string) => {
    setChecked(prev => ({ ...prev, [code]: !prev[code] }))
  }, [])

  const handleToggleAll = useCallback(() => {
    setChecked(prev => {
      const next: Record<string, boolean> = {}
      const checkedNow = sortedRows.filter(s => !!prev[s.code]).length
      const flipToCheckAll = sortedRows.length > 0 && checkedNow * 2 < sortedRows.length
      const target = flipToCheckAll
      for (const s of sortedRows) next[s.code] = target
      return next
    })
  }, [sortedRows])

  const handlePrescribersToggle = useCallback(() => {
    setChecked(prev => {
      const presRows = sortedRows.filter(s => s.can_prescribe)
      const allOn = presRows.length > 0 && presRows.every(s => !!prev[s.code])
      const next: Record<string, boolean> = { ...prev }
      if (allOn) {
        for (const s of presRows) next[s.code] = false
      } else {
        for (const s of sortedRows) next[s.code] = !!s.can_prescribe
      }
      return next
    })
  }, [sortedRows])

  const handleHomeopathicToggle = useCallback(() => {
    setChecked(prev => {
      const homeoRows = sortedRows.filter(s => s.homeopathic)
      const allOn = homeoRows.length > 0 && homeoRows.every(s => !!prev[s.code])
      const next: Record<string, boolean> = { ...prev }
      if (allOn) {
        for (const s of homeoRows) next[s.code] = false
      } else {
        for (const s of sortedRows) next[s.code] = !!s.homeopathic
      }
      return next
    })
  }, [sortedRows])

  const handleApplyClick = useCallback(() => {
    if (!isDirty) {
      setPopupOpen(true)
      return
    }
    window.parent.postMessage({ type: 'gui:filter-apply-click' }, '*')
    setAppliedCodes(currentCodes)
    setPopupOpen(false)
  }, [isDirty, currentCodes])

  const closePopup = useCallback(() => setPopupOpen(false), [])

  return {
    specialties,
    sortedRows,
    checked,
    allPossibleCount,
    allPrescribersCount,
    yourChoicesCount,
    labelIsCheckAll,
    uncheckAllDisabled,
    prescribersChecked,
    homeopathicChecked,
    isDirty,
    popupOpen,
    noChangesText: NO_CHANGES_POPUP_TEXT,
    toggleRow,
    handleToggleAll,
    handlePrescribersToggle,
    handleHomeopathicToggle,
    handleApplyClick,
    closePopup,
  }
}
