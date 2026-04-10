// Copyright (c) 2026 Skip Snow. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Selection state manager — available/selected/garbage with max limit.
// FC-SELECT-001: Provider selection for EvaluateCare handoff.
//
// State survives pagination and filter changes.
// Garbage flushes on new chat question.

import { useReducer, useCallback } from 'react'
import type { Provider, SelectionState, SelectionAction } from '../types/provider'

const MAX_SELECTED = 5

function selectionReducer(state: SelectionState, action: SelectionAction): SelectionState {
  switch (action.type) {
    case 'SET_AVAILABLE': {
      // New providers from search — merge with existing state
      // Don't touch selected or garbage — they persist
      const selectedNpis = new Set(state.selected.map(p => p.npi))
      const garbageNpis = new Set(state.garbage.map(p => p.npi))
      const available = action.providers.filter(
        p => !selectedNpis.has(p.npi) && !garbageNpis.has(p.npi)
      )
      return { ...state, available }
    }

    case 'SELECT': {
      if (state.selected.length >= state.maxSelected) return state
      const provider = state.available.find(p => p.npi === action.npi)
      if (!provider) return state
      return {
        ...state,
        available: state.available.filter(p => p.npi !== action.npi),
        selected: [...state.selected, provider],
      }
    }

    case 'DESELECT': {
      const provider = state.selected.find(p => p.npi === action.npi)
      if (!provider) return state
      return {
        ...state,
        selected: state.selected.filter(p => p.npi !== action.npi),
        available: [...state.available, provider],
      }
    }

    case 'DISMISS': {
      const fromAvailable = state.available.find(p => p.npi === action.npi)
      if (fromAvailable) {
        return {
          ...state,
          available: state.available.filter(p => p.npi !== action.npi),
          garbage: [...state.garbage, fromAvailable],
        }
      }
      // Dismiss from selected (filtered-out provider user chose to remove)
      const fromSelected = state.selected.find(p => p.npi === action.npi)
      if (fromSelected) {
        return {
          ...state,
          selected: state.selected.filter(p => p.npi !== action.npi),
          garbage: [...state.garbage, fromSelected],
        }
      }
      return state
    }

    case 'FLUSH_GARBAGE': {
      // New question asked — garbage returns to available pool
      return {
        ...state,
        available: [...state.available, ...state.garbage],
        garbage: [],
      }
    }

    case 'KEEP_FILTERED': {
      // User chose to keep a filtered-out selected provider — no state change needed
      // The UI just stops highlighting it
      return state
    }

    case 'REMOVE_FILTERED': {
      // Same as DISMISS from selected
      return selectionReducer(state, { type: 'DISMISS', npi: action.npi })
    }

    default:
      return state
  }
}

export function useSelectionState() {
  const [state, dispatch] = useReducer(selectionReducer, {
    available: [],
    selected: [],
    garbage: [],
    maxSelected: MAX_SELECTED,
  })

  const setAvailable = useCallback((providers: Provider[]) => {
    dispatch({ type: 'SET_AVAILABLE', providers })
  }, [])

  const select = useCallback((npi: string) => {
    dispatch({ type: 'SELECT', npi })
  }, [])

  const deselect = useCallback((npi: string) => {
    dispatch({ type: 'DESELECT', npi })
  }, [])

  const dismiss = useCallback((npi: string) => {
    dispatch({ type: 'DISMISS', npi })
  }, [])

  const flushGarbage = useCallback(() => {
    dispatch({ type: 'FLUSH_GARBAGE' })
  }, [])

  const keepFiltered = useCallback((npi: string) => {
    dispatch({ type: 'KEEP_FILTERED', npi })
  }, [])

  const removeFiltered = useCallback((npi: string) => {
    dispatch({ type: 'REMOVE_FILTERED', npi })
  }, [])

  return {
    state,
    setAvailable,
    select,
    deselect,
    dismiss,
    flushGarbage,
    keepFiltered,
    removeFiltered,
    isFull: state.selected.length >= state.maxSelected,
  }
}
