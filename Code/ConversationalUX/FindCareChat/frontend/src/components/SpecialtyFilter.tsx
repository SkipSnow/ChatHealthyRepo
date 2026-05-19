// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// SpecialtyFilter — React component implementing the master comprehensive UX
// described in EPIC-006-F-002-S-001-REQ-B-001 (with header geometry per
// EPIC-006-F-002-S-001-REQ-T-004 and per-element technical REQs T-005..T-015).
//
// Scope of this component (what it OWNS):
//   - per-row checked state (initial: all checked, per REQ-B-001)
//   - header macro-control state (Prescribers + Homeopathic checkboxes)
//   - Uncheck-All / Check-All label flip + disabled+tooltip when nothing checked
//   - sorting the visible list (list-one homeopathic specifics above list-two)
//
// Scope of this component (what it does NOT own):
//   - the Apply Filter button (rendered by the parent / wrapper)
//   - the cross-iframe postMessage plumbing
//   - the /classify API call (parent does that, passes `specialties` in)
//
// Submission rule: per REQ-B-001, ONLY the list of checked specialties
// determines what is submitted. The macro-control state is a manipulator of
// that checked-list, not an independent submission filter. The parent reads
// the checked list via the onSelectionChange callback when Apply Filter fires.

import { useState, useMemo, useEffect, useCallback } from 'react'
import './SpecialtyFilter.css'

// ── Public contract ────────────────────────────────────────────────
export interface SpecialtyRecord {
  /** Taxonomy code — stable key. */
  code: string
  /** Display name shown next to the checkbox. */
  name: string
  /** From /classify: this specialty can legally write prescriptions. */
  can_prescribe: boolean
  /** From /classify: this specialty is a Homeopathic provider (list-one or list-two). */
  homeopathic: boolean
  /**
   * From /classify: true when this row came from list-two (generalist
   * homeopaths plausibly applicable to the disorder), false/undefined when
   * it came from list-one (AI-matched specifics) OR is a non-homeopathic row.
   */
  homeopathic_general?: boolean
  /** Backend-supplied rank for tie-breaking inside list-one and list-two. */
  rank: number
}

export interface SpecialtyFilterProps {
  /** Full result set from /classify, already enriched with flags. */
  specialties: SpecialtyRecord[]
  /**
   * Reports the current checked-codes list on every change so the parent
   * can submit whatever's checked when Apply Filter fires. The parent
   * defines its own "no changes since last apply" semantics; this
   * component just emits authoritatively whenever its state moves.
   */
  onSelectionChange: (checkedCodes: string[]) => void
  /**
   * Optional. When provided, the title row renders a 4th cell containing
   * a "Providers" close button (visible on phone viewports only). The
   * frame host wires this to the parent so the user can leave the
   * filter overlay back to the providers screen.
   */
  onCloseRequest?: () => void
}

// ── Component ──────────────────────────────────────────────────────
export default function SpecialtyFilter({
  specialties,
  onSelectionChange,
  onCloseRequest,
}: SpecialtyFilterProps) {
  // Per-row checked state — code -> bool.
  // REQ-B-001 initial state: Prescribers macro is
  // CHECKED by default. The Prescribers macro semantics (force all
  // prescribers ON, all non-prescribers OFF) determine the initial
  // per-row state — so list-one prescribers (which are V5 picks with
  // can_prescribe=true) start CHECKED, list-one non-prescribers and the
  // list-two homeopathic generalists (almost all non-prescribers) start
  // UNCHECKED but remain visible.
  const [checked, setChecked] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {}
    for (const s of specialties) initial[s.code] = !!s.can_prescribe
    return initial
  })

  // Macro checkboxes are DERIVED: each reflects
  // whether its category is currently all-checked. Click toggles the
  // category (force-on if currently not all-checked, force-off if it
  // currently is). Removed the local state shadow that could drift
  // from the row state.

  // If the SET of specialty codes changes (a new /classify response replaces
  // the cache per REQ-B-001 "Cache Results on client"), reset per-row state
  // to the Prescribers-macro-checked default and reset the macro checkboxes
  // accordingly. Depend on a CONTENT key (the sorted-codes string), not the
  // array reference — parents re-render and pass a fresh array on every
  // interaction, and resetting on reference change would clobber user
  // check/uncheck actions.
  const specialtiesKey = useMemo(
    () => specialties.map(s => s.code).sort().join('|'),
    [specialties],
  )
  useEffect(() => {
    const fresh: Record<string, boolean> = {}
    for (const s of specialties) fresh[s.code] = !!s.can_prescribe
    setChecked(fresh)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specialtiesKey])

  // Sort: list-one homeopathic specifics above list-two generalists, then by
  // rank ascending within each tier. Non-homeopathic rows keep their backend
  // rank order and are interleaved by rank with list-one (which is the
  // default for the agent-flagged open issue — see [AGENT-FLAG] in spec).
  //
  // TODO(spec-clarify): [AGENT-FLAG] in EPIC-006-F-002-S-001-REQ-B-001 leaves
  // OPEN: (a) one merged ranked list vs. two visual sections, (b) which side
  // supplies the rank that puts natural fits at the top, (c) list-two
  // pre-fetched vs on-demand. Defaulted here to: single merged list, sorted
  // by (homeopathic_general flag ASC so list-one floats above list-two)
  // then by backend rank ASC, list-two assumed pre-fetched in the initial
  // /classify response. Revisit when the flag is resolved.
  const sortedRows = useMemo(() => {
    return [...specialties].sort((a, b) => {
      const aGen = a.homeopathic_general ? 1 : 0
      const bGen = b.homeopathic_general ? 1 : 0
      if (aGen !== bGen) return aGen - bGen
      return (a.rank ?? 0) - (b.rank ?? 0)
    })
  }, [specialties])

  // Header counts (REQ-T-005 / T-006 / T-007 + REQ-B-001 definitions).
  // "All possible" = every specialty type that can give care (= every row
  // delivered by /classify, since the backend already filtered to those).
  // "All prescribers" = subset of "All possible" with can_prescribe true.
  // "Your choices" = currently-checked count.
  const allPossibleCount = specialties.length
  const allPrescribersCount = useMemo(
    () => specialties.filter(s => s.can_prescribe).length,
    [specialties],
  )
  const yourChoicesCount = useMemo(
    () => Object.values(checked).filter(Boolean).length,
    [checked],
  )

  // Uncheck-All / Check-All label flip:
  // the button is an OPPOSITE-ACTION control. When most rows are checked,
  // the natural action is to clear → label "Uncheck All". When most rows
  // are unchecked, the natural action is to fill → label "Check All".
  // At exactly 50% checked, label stays "Uncheck All" (spec tie-break).
  const total = sortedRows.length
  const uncheckedCount = total - yourChoicesCount
  // checkedCount*2 < total is "strictly less than half checked" — at
  // exactly half the label stays "Uncheck All". Below half → "Check All".
  const labelIsCheckAll = total > 0 && yourChoicesCount * 2 < total

  // Macro state derived from row state — Prescribers macro is CHECKED iff
  // every prescriber row is currently checked. Same for Homeopathic.
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

  // Disabled state for the Uncheck-All button per REQ-B-001: when no
  // specialties are checked, greyed out with tooltip.
  const uncheckAllDisabled = !labelIsCheckAll && yourChoicesCount === 0

  // ── Effect: emit checked codes upward whenever they change ──────
  useEffect(() => {
    const codes: string[] = []
    for (const s of sortedRows) if (checked[s.code]) codes.push(s.code)
    onSelectionChange(codes)
    // Intentionally omit onSelectionChange from deps — parents commonly
    // pass an inline callback and we don't want to refire on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked, sortedRows])

  // ── Handlers ────────────────────────────────────────────────────
  const toggleRow = useCallback((code: string) => {
    setChecked(prev => ({ ...prev, [code]: !prev[code] }))
  }, [])

  const handleToggleAll = useCallback(() => {
    setChecked(prev => {
      const next: Record<string, boolean> = {}
      // Re-derive label on the latest snapshot so we don't act on a stale
      // render. Label is "Check All"
      // when STRICTLY less than half are checked → click checks all.
      // Otherwise label is "Uncheck All" → click unchecks all.
      const checkedNow = sortedRows.filter(s => !!prev[s.code]).length
      const flipToCheckAll = sortedRows.length > 0 && checkedNow * 2 < sortedRows.length
      const target = flipToCheckAll
      for (const s of sortedRows) next[s.code] = target
      return next
    })
    // Macro display is derived from row state; no separate reset needed.
  }, [sortedRows])

  // Prescribers macro click — opposite-action toggle:
  //   if currently CHECKED (all prescribers checked) → uncheck all prescribers
  //   if currently UNCHECKED → check all prescribers + uncheck non-prescribers
  //                            (the original "force the category" semantic)
  // Macro display state is derived from row state (see prescribersChecked
  // useMemo above), so we don't store macro state separately.
  const handlePrescribersToggle = useCallback(() => {
    setChecked(prev => {
      const presRows = sortedRows.filter(s => s.can_prescribe)
      const allOn = presRows.length > 0 && presRows.every(s => !!prev[s.code])
      const next: Record<string, boolean> = { ...prev }
      if (allOn) {
        // Currently CHECKED → uncheck all prescribers.
        for (const s of presRows) next[s.code] = false
      } else {
        // Currently UNCHECKED → force category on, opposite category off.
        for (const s of sortedRows) next[s.code] = !!s.can_prescribe
      }
      return next
    })
  }, [sortedRows])

  // Homeopathic macro click — symmetric opposite-action toggle:
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

  // ── Render ──────────────────────────────────────────────────────
  return (
    <div className="specialty-filter" data-testid="specialty-filter">
      <div className="specialty-filter__header">
        <div className="specialty-filter__title-row">
          <div className="specialty-filter__title-cell">
            <span className="specialty-filter__title">Choose Specialties</span>
          </div>
          <div className="specialty-filter__close-cell">
            {onCloseRequest && (
              <button
                type="button"
                className="specialty-filter__close-btn"
                data-testid="specialty-filter-close"
                onClick={onCloseRequest}
              >Providers</button>
            )}
          </div>
        </div>
        <div className="specialty-filter__count-row">
          <div className="specialty-filter__count-cell" data-testid="count-all-possible">
            <span className="specialty-filter__count-label">All possible</span>
            <span className="specialty-filter__count-value">{allPossibleCount}</span>
          </div>
          <div className="specialty-filter__count-cell" data-testid="count-all-prescribers">
            <span className="specialty-filter__count-label">All prescribers</span>
            <span className="specialty-filter__count-value">{allPrescribersCount}</span>
          </div>
          <div className="specialty-filter__count-cell" data-testid="count-your-choices">
            <span className="specialty-filter__count-label">Your choices</span>
            <span className="specialty-filter__count-value specialty-filter__count-value--your-choices">
              {yourChoicesCount}
            </span>
          </div>
        </div>
        <div className="specialty-filter__controls-row">
          <div className="specialty-filter__toggle-all-cell">
            <button
              type="button"
              className="specialty-filter__toggle-all-btn"
              onClick={handleToggleAll}
              disabled={uncheckAllDisabled}
              title={uncheckAllDisabled ? 'No specialties are checked' : ''}
              data-testid="toggle-all-button"
            >
              {labelIsCheckAll ? 'Check All' : 'Uncheck All'}
            </button>
          </div>
          <div className="specialty-filter__macro-checkbox-cell">
            <label className="specialty-filter__macro-checkbox">
              <input
                type="checkbox"
                checked={prescribersChecked}
                onChange={() => handlePrescribersToggle()}
                data-testid="macro-prescribers"
              />
              Prescribers
            </label>
            <label className="specialty-filter__macro-checkbox">
              <input
                type="checkbox"
                checked={homeopathicChecked}
                onChange={() => handleHomeopathicToggle()}
                data-testid="macro-homeopathic"
              />
              Homeopathic
            </label>
          </div>
        </div>
      </div>

      {/* Body — one horizontal row per specialty (REQ-T-012, REQ-T-013) */}
      <div className="specialty-filter__body" data-testid="specialty-list">
        {sortedRows.map(s => (
          <div
            key={s.code}
            className="specialty-filter__row"
            data-spec-code={s.code}
            data-can-prescribe={s.can_prescribe ? 'true' : 'false'}
            data-homeopathic={s.homeopathic ? 'true' : 'false'}
            data-homeopathic-general={s.homeopathic_general ? 'true' : 'false'}
            onClick={() => toggleRow(s.code)}
            role="checkbox"
            aria-checked={!!checked[s.code]}
            tabIndex={0}
            onKeyDown={e => {
              if (e.key === ' ' || e.key === 'Enter') {
                e.preventDefault()
                toggleRow(s.code)
              }
            }}
            style={{ cursor: 'pointer' }}
          >
            <span className="specialty-filter__row-name">{s.name}</span>
            <input
              type="checkbox"
              className="specialty-filter__row-checkbox"
              checked={!!checked[s.code]}
              // Display-only. Toggle is owned by the row's onClick above.
              // readOnly prevents the browser from emitting a change event
              // we'd otherwise have to coordinate with the row handler.
              readOnly
              aria-label={s.name}
              tabIndex={-1}
            />
          </div>
        ))}
      </div>
    </div>
  )
}
