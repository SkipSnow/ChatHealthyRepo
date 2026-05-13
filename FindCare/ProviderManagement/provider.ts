// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Shared provider types — used by FindCare, EvaluateCare, and UX components.

export interface Provider {
  npi: string
  name: string
  address?: string
  city?: string
  state?: string
  zip?: string
  county?: string
  phone?: string
  specialty?: string
  primary_specialty?: string
  can_prescribe?: boolean
  homeopathic?: boolean
  taxonomy_code?: string
}

export interface FilterOption {
  code: string
  name: string
  can_prescribe?: boolean
  homeopathic?: boolean
  count?: number
}

export interface SelectionState {
  available: Provider[]
  selected: Provider[]
  garbage: Provider[]
  maxSelected: number
}

export type SelectionAction =
  | { type: 'SET_AVAILABLE'; providers: Provider[] }
  | { type: 'SELECT'; npi: string }
  | { type: 'DESELECT'; npi: string }
  | { type: 'DISMISS'; npi: string }
  | { type: 'FLUSH_GARBAGE' }
  | { type: 'KEEP_FILTERED'; npi: string }
  | { type: 'REMOVE_FILTERED'; npi: string }

export interface FilterState {
  prescribersOnly: boolean
  homeopathicOnly: boolean
  checkedCodes: Set<string>
}
