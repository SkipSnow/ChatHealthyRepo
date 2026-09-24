# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The versioned-collection binding state, held apart from the FastAPI router
and Mongo-reading code in runtime_data_collections.

mongo_utilities._version_map() reads _state.bases to resolve a base collection
name to its bound versioned name. That resolver runs on every collection index,
including inside Azure Automation runbooks, whose sandbox has no FastAPI. Keeping
_State/_state here -- with no third-party imports -- lets the resolver (and the
runbook that inlines it) read the binding state without dragging FastAPI along.
runtime_data_collections re-exports these names, so existing importers are
unchanged.
"""
from __future__ import annotations


class _State:
    target_id: str | None = None
    env: str | None = None
    # slot -> composed 'Database.Collection_v_N'. What every consumer of a
    # bound collection reads.
    bindings: dict[str, str] = {}
    # (database, base) -> composed name, taken from the base the RECORD
    # states. A base is a fact the binding carries, not a fact recovered from a
    # string.
    bases: dict[tuple[str, str], str] = {}
    # The parameter declaration for this env: pages[] and carry_over[].
    tool_configuration: dict = {}


_state = _State()
