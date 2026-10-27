# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007, LLD v83 III.10) -- attach each molecule
MASTER's fda block from its molecule-keyed FDA record.

The FDA collection and the master share the key (_id = molecule_id, LLD v83
III.3 decision #7), so the attach is a point lookup per master: copy the FDA
record's assembled facts -- indications, drug_class SET, identity (rxcui/unii/
substance_name union), usage, trust aggregate -- onto the master's fda block.
Serial: ~1,700 masters, one pass, single-writer. A master with no FDA record
keeps its stub fda block and is recorded as a finding (non-fatal).

The normalized indication SET for the evaluation search is attached from the
NormalizedIndicationMap (EPIC-010-F-007-S-008, deferred); until it lands, the
master's fda.indications carries the FDA-label indications only.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_IDENTITY_FIELDS = ("rxcui", "unii", "substance_name")


def fda_block_from(fda_doc: dict) -> dict:
    """The record's fda block: drug_class as the SET, and identity as the
    unioned standard identifiers.

    Indications and the usage text are molecule-level and live on the FDA
    record, reachable by the identity carried here. A record holds each
    molecule-level fact once.
    """
    molecule = fda_doc.get("molecule") or {}
    return {
        "drug_class": list((fda_doc.get("drug_class") or {}).get("classes") or []),
        "identity": {k: list(molecule.get(k) or []) for k in _IDENTITY_FIELDS},
    }


