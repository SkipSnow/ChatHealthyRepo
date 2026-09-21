# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Shared route support, drained out of app.py.

The page handlers own their route bodies; these are the helpers more than one
page leans on -- requirement checking, geography derivation, the page-parameter
read/clear a gesture needs, the refinement question, and the mined-value entry
builders. app.py collects a request and hands it to a page handler; neither
app.py nor the handlers redefine these.
"""
from __future__ import annotations

from chathealthy_lib import http_request_facts as request_facts
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.runtime_data_collections import (
    required_parameters, optional_parameters)

from db_config import (get_db, SESSION_DB, SESSION_COLLECTION,
                       INDIVIDUAL_PROVIDER_PAGE, FACILITY_PAGE,
                       CLINICAL_TRIAL_PAGE)
from page_parameters import parameter_entry
from ProviderManagement.utterance_mining import ask_for_missing

SEX_CODES = ("F", "M", "X", "U")


def unmet_requirements(tool: str, in_force: dict) -> list[str]:
    """Which of this page's declared requirements are not in force.

    Empty means the page can run. Otherwise these are the attributes to
    ask the person about, by name -- so a parameter made required in the
    declaration is asked for without a line of code being written for it.

    A value that is present but empty is not in force -- an empty string
    and a missing string are the same absence to the person who did not
    say it.
    """
    missing = []
    for name in required_parameters(tool):
        value = in_force.get(name)
        if value is None or value == "" or value == [] or value == {}:
            missing.append(name)
    return missing


def state_of_zip(zip_code: str) -> str:
    """The state a ZIP is in, read from the addresses we already hold.

    Derived, never queried on. The state is what makes the requirement
    met; the ZIP is what the person asked for, and it is narrower.
    """
    wanted = (zip_code or "").strip()
    if not wanted:
        return ""
    db = get_db()
    if db is None:
        return ""
    row = db["PublicHealthData"]["Provider"].find_one(
        {"practice_addresses.zip": wanted},
        {"practice_addresses.zip": 1, "practice_addresses.state": 1})
    for address in ((row or {}).get("practice_addresses") or []):
        if str(address.get("zip") or "").strip() == wanted:
            return str(address.get("state") or "").strip().upper()
    return ""


def geography_known(in_force: dict) -> dict:
    """What is known about the place, as against what was asked for.

    The requirement is that the state be KNOWN. A ZIP supplies it without
    the person repeating themselves, so the derived state is added here --
    and nowhere else, because the query is built from what was asked for.
    """
    known = dict(in_force)
    if not str(known.get("state") or "").strip():
        derived = state_of_zip(str(known.get("zip") or ""))
        if derived:
            known["state"] = derived
    return known


def read_page_parameter(page: str, name: str):
    """One of this page's own parameters, off the session."""
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message=f"the session is unreachable, so {page} cannot read what "
                    f"it needs to answer")
    address = f"userParameters.pages.{page}.{name}"
    doc = db[SESSION_DB][SESSION_COLLECTION].find_one(
        {"_id": request_facts.facts().session_guid()}, {address: 1})
    held = ((doc or {}).get("userParameters", {})
            .get("pages", {}).get(page, {}).get(name))
    return (held or {}).get("value") if isinstance(held, dict) else held


def gesture_entry(value) -> dict:
    """A value a gesture set, ready to be stored. The determination is a
    rule rather than a model: nobody inferred which record the person
    opened -- they opened it."""
    from chathealthy_lib.authentication.user_parameters import ParameterEntry
    return ParameterEntry(value=value, route="tool",
                          determination="rule").model_dump(exclude_none=True)


# Which attribute names the record a page is currently showing.
OPEN_RECORD_ATTRIBUTE = {
    INDIVIDUAL_PROVIDER_PAGE: "openNpi",
    FACILITY_PAGE: "openNpi",
    CLINICAL_TRIAL_PAGE: "openNctId",
}


def open_record_attribute(page: str) -> str:
    name = OPEN_RECORD_ATTRIBUTE.get(page)
    if not name:
        raise ChatHealthyException(
            mode="value_error",
            component="FindCareBackend",
            message=f"{page!r} shows no record, so nothing can be opened or "
                    f"closed on it")
    return name


def clear_page_parameter(page: str, name: str) -> None:
    """Take one of this page's own parameters off the session."""
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message=f"the session is unreachable, so {page} cannot record "
                    f"what it stopped showing")
    db[SESSION_DB][SESSION_COLLECTION].update_one(
        {"_id": request_facts.facts().session_guid()},
        {"$unset": {f"userParameters.pages.{page}.{name}": ""}})


async def question_for(tool: str, missing: list[str], in_force: dict,
                       utterance: str, history) -> str:
    """What to ask the person when this tool cannot run yet.

    The tool authors it rather than the gateway, because the tool holds the
    requirement. What it requires, what it merely accepts, and what the
    person has already said all reach the model as facts from the
    configuration -- so making a parameter Required is enough to have it
    asked for, with nothing written for it.
    """
    return await ask_for_missing(
        tool, missing, optional_parameters(tool), in_force,
        utterance, history,
        component="FindCareApp", call_site=f"{tool}_refinement_request")


def searched_codes(offered: list[dict], ticked: list[str]) -> list[str]:
    """Nothing ticked means nothing was narrowed, so the whole offered set
    applies."""
    return ticked or [row["code"] for row in offered]


def sex_code(mined_sex: str) -> str:
    """NPPES records sex as F, M, X (neither) or U (undisclosed). A model
    answering outside that set has not mined a sex, and storing the answer
    would put a value on the session that no reader of it can act on."""
    code = str(mined_sex or "").strip().upper()
    if not code:
        return ""
    if code not in SEX_CODES:
        raise ChatHealthyException(
            mode="value_error",
            component="FindCareBackend",
            message=f"{code!r} is not a sex code; NPPES uses "
                    f"{', '.join(SEX_CODES)}")
    return code


def provider_page_entries(mined, complaint: str, ticked: list[str]) -> dict:
    """The mined values as parameter entries, keyed by attribute."""
    entries: dict = {}
    for part, value in mined.geography.model_dump().items():
        if part in ("state", "city", "zip", "county") and value:
            entries[part] = parameter_entry(value)
    if complaint:
        entries["complaint"] = parameter_entry(complaint)
    name = mined.provider_name
    parts = {"last": name.last.strip().upper(),
             "first": name.first.strip().upper(),
             "middle": name.middle.strip().upper()}
    if any(parts.values()):
        entries["providerName"] = parameter_entry(parts)
    sex = sex_code(mined.provider_sex)
    if sex:
        entries["providerSex"] = parameter_entry(sex)
    if mined.sole_proprietor is not None:
        entries["soleProprietor"] = parameter_entry(bool(mined.sole_proprietor))
    if mined.insurance:
        entries["insurance"] = parameter_entry(mined.insurance)
    if ticked:
        entries["selectedSpecialtyCodes"] = parameter_entry(ticked)
    return entries
