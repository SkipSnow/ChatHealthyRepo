# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FindCare page-parameter store, drained from app.py.

Each page's own parameters -- what it mined and holds -- read and written
here, keyed to the request's own session facts so no caller carries a token
to reach the session. Drained out of app.py so the tools that own a page's
mining read and write through this store without importing the FastAPI app.
"""
from __future__ import annotations

from chathealthy_lib import http_request_facts as request_facts
from chathealthy_lib.exceptions import ChatHealthyException

from db_config import get_db, SESSION_DB, SESSION_COLLECTION


def parameter_entry(value) -> dict:
    """A mined value as a parameter entry, ready to be stored. The route is
    the tool because a tool mined it; the determination is the model because
    a model inferred it."""
    from chathealthy_lib.authentication.user_parameters import ParameterEntry
    return ParameterEntry(value=value, route="tool",
                          determination="model").model_dump(exclude_none=True)


def parameters_in_force(page: str) -> dict:
    """Every parameter this page holds, as plain values -- one read, so a
    caller cannot fetch only the fields it remembered."""
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message=f"the session is unreachable, so {page} cannot read what "
                    f"is in force")
    doc = db[SESSION_DB][SESSION_COLLECTION].find_one(
        {"_id": request_facts.facts().session_guid()},
        {f"userParameters.pages.{page}": 1})
    held = ((doc or {}).get("userParameters", {})
            .get("pages", {}).get(page, {})) or {}
    return {name: (entry.get("value") if isinstance(entry, dict) else entry)
            for name, entry in held.items()}


def write_page_parameters(page: str, entries: dict) -> None:
    """The page that mined a parameter writes it on its own page -- no other
    page of the session is addressed here, ever. The session is read from the
    facts this request arrived with."""
    if not entries:
        return
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message=f"{page} parameters mined but the session is unreachable "
                    f"to write them to")
    guid = request_facts.facts().session_guid()
    result = db[SESSION_DB][SESSION_COLLECTION].update_one(
        {"_id": guid},
        {"$set": {f"userParameters.pages.{page}.{name}": value
                  for name, value in entries.items()}})
    if result.matched_count == 0:
        raise ChatHealthyException(
            mode="session_not_found",
            component="FindCareBackend",
            message=f"no session {guid!r} to write the mined {page} "
                    f"parameters to")
