"""The one live set of user parameters on a session.

Two things live on a session and they answer different questions.
session_conversation_history is the HISTORY: an ordered list of tool
invocations with their inputs and outputs, written by run_and_log. This is
the STATE: what is true right now.

Neither is derived from the other. Nothing reads the history to decide
anything, and nothing keeps a version, a timestamp or a provenance field in
here -- if you want to know how a value got where it is, the history names
the tool that wrote it.

A parameter is defined once and means the same thing everywhere. Geography
is geography whether a provider search or a clinical-trial search reads it,
which is what lets a user say "New York" while looking at trials and then
ask for doctors without saying it again.

Every value is a validated model rather than JSON in a string. The shape
this replaces carried geography as a JSON string inside an argument, so a
malformed value decoded to {} and the search silently widened to the whole
country instead of failing.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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


class UserParameters(BaseModel):
    """Live values. Current only; no history, no metadata.

    A tool reads whichever keys it needs and writes none directly -- writes
    go through the parameters tool, so validation happens in one place and
    the change lands in the history for free.

    Adding a refinement variable is a field here plus a read in the tool
    that wants it. Nothing else moves, and no other structure carries a
    second copy of it.
    """

    geography: Optional[Geography] = None

    # A provider named outright, rather than searched for by what they do.
    # Stored uppercase because the records are: NPPES holds every name in
    # upper case, so an uppercased term matches without the case-insensitive
    # option that would make the index unusable.
    provider_name: Optional[ProviderName] = None

    # Which insurance the person carries. This narrows to providers who
    # list that payer among their identifiers -- it does NOT establish that
    # a plan's network includes them, and nothing in this data can.
    insurance: Optional[str] = None

    # A stated preference about the provider, not a fact about the search.
    # Absent means no preference, which is not the same as "any": a stated
    # preference excludes everyone who does not match it, including
    # providers who disclosed nothing.
    #
    #   F  Female        M  Male
    #   X  Neither Male nor Female -- an affirmation the provider made
    #   U  Undisclosed             -- a refusal to stipulate
    #
    # The two are never merged. One provider told us something and the
    # other declined, and collapsing them would misrepresent both.
    provider_sex: Optional[str] = None

    # Y when the person wants a provider practising on their own account.
    sole_proprietor: Optional[bool] = None

    # The clinical concept the utterance manager translated the user's words
    # into. NEVER the words themselves: "shrink" is something a person said
    # and names no specialty a payer or a provider record recognises. What
    # lands here is what it means -- "Psychological or psychiatric service
    # provider" -- which is a fact the specialty step can act on.
    #
    # The utterance stays in the conversation. This is its translation, and
    # the translation is the parameter.
    complaint: str = ""

    # What the specialty step offered, and what the user kept.
    #
    # These are the translated fact, not the words that produced it. "Shrink"
    # is something a person said: it has no clinical or reimbursement
    # meaning and cannot be queried. The utterance belongs to the
    # conversation; the LLM turns it into NUCC codes -- which do mean
    # something to a payer and to a provider record -- and those are the
    # parameter.
    #
    # The universe is set by the specialty tool; the selection is set by the
    # user. Neither recomputes the other.
    specialties: list[Specialty] = Field(default_factory=list)
    selected_specialty_codes: list[str] = Field(default_factory=list)

    # WHERE the user is in each list they are reading: one function, one
    # key.
    #
    # A page number would name a place the query cannot go back to, because
    # the searches are keyset-paged. But the key is enough on its own -- the
    # query is ORDERED, so the key IS the position, and asking for the rows
    # after it returns that page in one hop. A chain of every cursor walked
    # to get there describes how the user arrived, which is not what
    # returning needs.
    #
    # Keyed by the function that is paging -- findAProvider,
    # findClinicalTrials -- so each holds its own place and moving between
    # them disturbs neither. That is what makes switching back and forth
    # survivable: every list is still where it was left.
    #
    # A missing key means the top of that list, which is where a function
    # that has not been paged yet correctly starts.
    page_cursors: dict[str, str] = Field(default_factory=dict)

    # The provider whose detail is open, if one is. A detail is a place the
    # user navigated to and expects to still be at when they come back;
    # holding it anywhere but here would make it the one thing about their
    # position that a context switch forgets.
    selected_provider_npi: str = ""

    def selected_or_all(self) -> list[str]:
        """The codes a search should use.

        No selection means the user has not narrowed, so the whole offered
        set applies. An empty selection made deliberately is a different
        thing and the caller decides how to treat it.
        """
        if self.selected_specialty_codes:
            return list(self.selected_specialty_codes)
        return [s.code for s in self.specialties if s.code]
