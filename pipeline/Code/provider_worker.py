# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
from __future__ import annotations

from state_filter import is_full_load

_NPPES_STATE_FIELDS = (
    "Provider Business Mailing Address State Name",
    "Provider Business Practice Location Address State Name",
    "Provider License Number State Code_1",
)


def _raw_row_matches_state(raw: dict, states: list[str]) -> bool:
    if is_full_load(states):
        return True
    wanted = {s.upper() for s in states if s}
    for field in _NPPES_STATE_FIELDS:
        val = (raw.get(field) or "").strip().upper()
        if val and val in wanted:
            return True
    return False
