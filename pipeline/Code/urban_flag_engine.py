# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Stamp the urban marker — LLD v47 §5.2.15.

Two markers, from the RUCC county_enrichment has already resolved:

    practice_addresses[i].county.urban   per address
    urban                                per provider

Per provider the marker is the MOST urban of all the provider's practice
addresses. Someone practising in three counties is urban if any one of them
is metropolitan, because they can be reached there. Most urban is the LOWEST
RUCC — the scale runs 1 for a metro area of a million and up to 9 for rural
and not adjacent to one — so the rollup is a minimum, not a maximum.

Absent, never false, where no RUCC resolved: false asserts rural of a
provider the pipeline has not placed, and EPIC-006-F-025 already tells the
Provider Detail panel how to render absent.

The whole state is one updateMany with an aggregation pipeline. No cursor
walks the providers and no document crosses the wire: the marker is a
function of a field each document already carries, so the server can derive
it in place.
"""
from __future__ import annotations

import time as _time

from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

URBAN_RUCC = [1, 2, 3]


def _urban_from(rucc_expr: dict | str) -> dict:
    """true / false / absent, from a RUCC expression."""
    return {
        "$cond": [
            {"$eq": [{"$type": rucc_expr}, "missing"]},
            "$$REMOVE",
            {"$in": [rucc_expr, URBAN_RUCC]},
        ]
    }


def urban_pipeline() -> list[dict]:
    """The server-side derivation, per provider document."""
    return [
        # Per address: rewrite each practice address with county.urban set
        # from its own county.rucc.
        {"$set": {
            "practice_addresses": {
                "$map": {
                    "input": {"$ifNull": ["$practice_addresses", []]},
                    "as": "a",
                    "in": {
                        "$mergeObjects": [
                            "$$a",
                            {"county": {"$mergeObjects": [
                                {"$ifNull": ["$$a.county", {}]},
                                {"urban": _urban_from("$$a.county.rucc")},
                            ]}},
                        ]
                    },
                }
            }
        }},
        # Per provider: the lowest RUCC across those addresses. $min ignores
        # missing values, and returns null when every one of them is missing,
        # which is the case that must leave the marker absent.
        {"$set": {
            "urban": {
                "$let": {
                    "vars": {"lowest": {"$min": "$practice_addresses.county.rucc"}},
                    "in": {
                        "$cond": [
                            {"$eq": [{"$type": "$$lowest"}, "missing"]},
                            "$$REMOVE",
                            {"$cond": [
                                {"$eq": ["$$lowest", None]},
                                "$$REMOVE",
                                {"$in": ["$$lowest", URBAN_RUCC]},
                            ]},
                        ]
                    },
                }
            }
        }},
    ]


def apply_urban_flags(config: dict, *, mongo=None, blob=None) -> dict:
    """Stamp both markers for one state partition."""
    from steps._partitions import business_state_filter  # noqa: PLC0415

    run_id = config["run_id"]
    provider_collection = config["provider_collection"]
    state = (config.get("partition_state") or "").upper() or None
    db_name, coll_name = str(provider_collection).split(".", 1)
    coll = mongo[db_name][coll_name]

    query = {"run_id": run_id}
    query.update(business_state_filter(state))

    started = _time.time()
    _log.info("urban_flag[%s]: stamping %s", state or "ALL", provider_collection)
    result = coll.update_many(query, urban_pipeline())
    elapsed = _time.time() - started

    urban_true = coll.count_documents({**query, "urban": True})
    urban_false = coll.count_documents({**query, "urban": False})
    absent = coll.count_documents({**query, "urban": {"$exists": False}})
    _log.info("urban_flag[%s]: matched=%s modified=%s urban=%s rural=%s "
              "unplaced=%s (%.1fs)", state or "ALL", f"{result.matched_count:,}",
              f"{result.modified_count:,}", f"{urban_true:,}", f"{urban_false:,}",
              f"{absent:,}", elapsed)
    return {
        "state": state,
        "matched": result.matched_count,
        "modified": result.modified_count,
        "urban": urban_true,
        "rural": urban_false,
        "unplaced": absent,
    }
