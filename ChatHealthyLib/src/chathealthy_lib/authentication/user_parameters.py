"""The one live set of user parameters on a session.

Two things live on a session and they answer different questions.
session_conversation_history is the HISTORY: an ordered list of tool
invocations with their inputs and outputs, written by run_and_log. This is
the STATE: what is true right now.

A parameter name is a pair, not a string: the page and the attribute.
There is no short form and no default page, because a default page is the
flat model with a longer spelling -- a name that resolves without a page
is a name whose scope depends on where it was written.

Parameters are held in one map per page, so a write addresses one page's
map and cannot reach another's; there is no shared slot for a write to
collide in. Three values more than one page needs -- geography, the
complaint and the chosen specialty codes -- are one parameter per page
that needs them, and they are distinct values that happen to share a name.

Every value is a validated model rather than JSON in a string. The shape
this replaces carried geography as a JSON string inside an argument, so a
malformed value decoded to {} and the search silently widened to the whole
country instead of failing.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# The four things a person can hold an answer about. The set is closed: a
# fifth page is a new feature with its own requirements, so it is fixed in
# the code that validates the declaration rather than held as data, since
# a closed set held as data is a set an edit can open
# (EPIC-006-F-008-S-001-REQ-B-002).
PAGES: tuple[str, ...] = ("facility", "individualProvider", "NUCC", "clinicalTrial")


class Geography(BaseModel):
    """Where the user is looking. One of these, whoever set it.

    Every field optional because a user names what they name: a state, or a
    city and state, or a zip alone. What counts as sufficient is the
    consuming tool's rule, not this model's.
    """

    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None

    def is_empty(self) -> bool:
        return not any((self.state, self.city, self.county, self.zip))


class ProviderName(BaseModel):
    """A provider named outright.

    last is matched exactly. first and middle match the name or the bare
    initial in either direction, because a record may hold JAMES where the
    person typed J, or hold J where they typed JAMES -- and a prefix only
    ever resolves the first of those.
    """

    last: str = ""
    first: str = ""
    middle: str = ""

    def is_empty(self) -> bool:
        return not any((self.last, self.first, self.middle))


class Specialty(BaseModel):
    """One NUCC specialty as the panel shows it."""

    code: str
    name: str = ""
    can_prescribe: bool = False
    homeopathic: bool = False
    rank: int = 0


class ParameterEntry(BaseModel):
    """One parameter in force, and the facts about the write that set it.

    Route, determination and source page live here rather than in the
    invocation history, which is what makes them readable at the moment
    the parameter is read.
    """

    value: Any = None
    # Which of the three routes wrote it (S-003-REQ-B-005).
    route: Literal["tool", "gateway", "utterance_manager"] = "gateway"
    # Whether a rule determined it or a model inferred it (S-003-REQ-B-006).
    # Independent of the route: the gateway can carry a model-inferred
    # value and the utterance manager a rule-determined one.
    determination: Literal["rule", "model"] = "rule"
    # The source page when it arrived by carry-over (S-005-REQ-B-008).
    # Absent when the person or a tool set it here.
    carried_from: Optional[str] = None


# Every flat name the previous model accepted, and where it goes. A
# name absent from this table was withdrawn: page_cursors was the flat
# model expressing page scope inside a value, and a position is per
# page and becomes each page's own position attribute.
_DISPOSITION: dict[str, tuple[tuple[str, str], ...]] = {
    "geography": (("individualProvider", "geography"),
                  ("facility", "geography")),
    "complaint": (("NUCC", "complaint"),
                  ("individualProvider", "complaint")),
    "specialties": (("NUCC", "offeredSpecialties"),),
    "selected_specialty_codes": (("NUCC", "selectedSpecialtyCodes"),
                                 ("individualProvider",
                                  "selectedSpecialtyCodes")),
    "provider_name": (("individualProvider", "providerName"),),
    "insurance": (("individualProvider", "insurance"),),
    "provider_sex": (("individualProvider", "providerSex"),),
    "sole_proprietor": (("individualProvider", "soleProprietor"),),
    "selected_provider_npi": (("individualProvider", "openNpi"),),
}


class UserParameters(BaseModel):
    """A map of page to a map of attribute to entry.

    Setting, clearing or reading a parameter on one page has no effect on
    any parameter of any other page, structurally rather than by a rule
    applied on top: there is no shared slot for a write to collide in.
    """

    pages: dict[str, dict[str, ParameterEntry]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _convert_flat_shape(cls, data: Any) -> Any:
        """A session already holding the old shape, converted on read.

        There is no migration job. Each flat name becomes its
        page-qualified successor, a name whose disposition is withdrawal is
        dropped, and the converted shape is written back by the next
        ordinary write. A session never touched again expires holding the
        old shape and is never read again, which is why converting on read
        costs nothing and converting every document would.
        """
        if not isinstance(data, dict) or "pages" in data:
            return data
        pages: dict[str, dict[str, dict]] = {}
        for flat_name, destinations in _DISPOSITION.items():
            if flat_name not in data:
                continue
            value = data[flat_name]
            if value is None or value == "" or value == [] or value == {}:
                continue
            for page, attribute in destinations:
                pages.setdefault(page, {})[attribute] = {
                    "value": value,
                    # The route that hydrated it. The old shape recorded no
                    # determination on the parameter, so the write it stands
                    # for is a rule.
                    "route": "gateway",
                    "determination": "rule",
                }
        return {"pages": pages}

    def get(self, page: str, attribute: str) -> Any:
        """The value in force, or None."""
        entry = (self.pages.get(page) or {}).get(attribute)
        return entry.value if entry is not None else None

    def entry(self, page: str, attribute: str) -> Optional[ParameterEntry]:
        return (self.pages.get(page) or {}).get(attribute)

    def has(self, page: str, attribute: str) -> bool:
        """Whether the attribute carries a value. "In force" is having one."""
        entry = self.entry(page, attribute)
        if entry is None:
            return False
        value = entry.value
        if value is None:
            return False
        if isinstance(value, (str, list, dict, tuple)) and len(value) == 0:
            return False
        return True

    def set(self, page: str, attribute: str, entry: ParameterEntry) -> None:
        self.pages.setdefault(page, {})[attribute] = entry

    def clear(self, page: str, attribute: str) -> None:
        (self.pages.get(page) or {}).pop(attribute, None)

    def clear_page(self, page: str) -> list[str]:
        cleared = sorted(self.pages.get(page) or {})
        self.pages.pop(page, None)
        return cleared

    def clear_all(self) -> list[str]:
        cleared = [f"{page}.{attribute}"
                   for page, attributes in sorted(self.pages.items())
                   for attribute in sorted(attributes)]
        self.pages = {}
        return cleared
