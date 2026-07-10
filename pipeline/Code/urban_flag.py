# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""urban_flag — stamp address.urban from RUCC using practice-address county FIPS."""

from __future__ import annotations

RUCC_JSON_BLOB_NAME = "rucc.json"
URBAN_RUCC_MAX = 3

from pipeline_db import get_mongo
from process_assignment_activity import _load_rucc, _stamp_urban


def execute(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = (partition.get("business_address_state") or partition.get("state") or "").upper()
    rucc = _load_rucc(ctx.env_prefix, ctx.config.get("staging_container", ctx.args.blob_container))
    db_name, coll_name = ctx.provider_collection.split(".", 1)
    coll = get_mongo()[db_name][coll_name]
    filt: dict = {"run_id": ctx.run_id}
    scope_states = [s.upper() for s in ctx.args.resolved_states()]
    if state:
        filt["addresses"] = {"$elemMatch": {"state": state}}
    elif scope_states and scope_states != ["ALL"]:
        filt["addresses"] = {"$elemMatch": {"state": {"$in": scope_states}}}

    stamped = 0
    for doc in coll.find(filt, batch_size=500):
        before = any(
            isinstance(a, dict) and a.get("urban") is not None
            for a in (doc.get("addresses") or [])
        )
        _stamp_urban(doc, rucc)
        coll.replace_one({"_id": doc["_id"]}, doc)
        if not before:
            stamped += 1
    return {"state": state, "stamped": stamped}
