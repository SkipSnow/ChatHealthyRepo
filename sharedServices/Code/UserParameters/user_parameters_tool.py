"""The one writer of the live user parameters.

Every other tool READS parameters off the session and writes none of its
own. Writes come here, which buys three things that were previously absent:
validation happens once rather than at each call site, a change lands in the
history for free because run_and_log records this tool like any other, and
"which tool set geography" is answerable without a provenance field on the
parameter.

Two ways in, one path through:

  deterministic     a panel control sets a key -- the user ticked a
                    specialty, chose a county, cleared a filter
  non_deterministic the utterance manager extracted a value from what the
                    user typed and calls this to record it

Both are the same write. The distinction is recorded so a session can be
read back to see which narrowing a model produced and which the user did
by hand.

Verbs are set / clear / clear_all. There is no "merge" verb: a parameter is
whatever the last thing to set it left there, and a caller that wants to
combine values does so before calling.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

import sys as _ch_sys
import pathlib as _ch_pl

for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break

from chathealthy_lib.authentication.agent_deps import AgentDeps  # noqa: E402
from chathealthy_lib.authentication.chathealthy_tool import ChatHealthyTool  # noqa: E402
from chathealthy_lib.authentication.user_parameters import (  # noqa: E402
    Geography, Specialty, UserParameters,
)
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

log = ChatHealthyLoggingService()

# A name not listed here is refused. The set of parameters is a declared
# thing: a tool inventing one would put a fact somewhere no other tool
# knows to look, which is the shape this design exists to remove.
#
# complaint holds the TRANSLATION, never the utterance: the utterance
# manager turns "shrink" into "Psychological or psychiatric service
# provider" and that is what is written. A raw colloquialism reaching this
# set would be a word nothing can query.
WRITABLE = ("geography", "complaint", "specialties", "selected_specialty_codes",
            "page_cursors", "selected_provider_npi")


class Request(BaseModel):
    verb: Literal["set", "clear", "clear_all", "read"] = Field(
        default="read",
        description="read returns the parameters; set writes one; clear "
                    "removes one; clear_all empties them.")
    name: Optional[str] = Field(
        default=None,
        description="Which parameter. One of: geography, complaint, "
                    "specialties, selected_specialty_codes, page_cursors, "
                    "selected_provider_npi. Any other name is refused.")
    value: Any = Field(
        default=None,
        description="The new value, validated into that parameter's shape.")
    origin: Literal["deterministic", "non_deterministic"] = Field(
        default="deterministic",
        description="Whether a rule set this value or a model inferred it.")


class Response(BaseModel):
    parameters: dict = Field(default_factory=dict)
    changed: list[str] = Field(default_factory=list)
    error: Optional[str] = None


def _coerce(name: str, value: Any):
    """Validate a value into its parameter's model. Raises on a bad shape.

    This is why the parameters are models rather than loose JSON: geography
    used to travel as a string inside an argument, so a malformed value
    decoded to an empty dict and the search widened to the whole country
    instead of refusing.
    """
    if value is None:
        return None
    if name == "geography":
        return value if isinstance(value, Geography) else Geography(**dict(value))
    if name == "specialties":
        return [s if isinstance(s, Specialty) else Specialty(**dict(s))
                for s in (value or [])]
    if name == "selected_specialty_codes":
        return [str(c) for c in (value or []) if str(c).strip()]
    if name == "page_cursors":
        # One key per function. A blank key is the top of that list, which
        # is what having no entry already means, so it is dropped rather
        # than stored as a second way of saying the same thing.
        return {str(fn): str(key).strip()
                for fn, key in dict(value or {}).items()
                if str(fn).strip() and str(key).strip()}
    if name in ("complaint", "selected_provider_npi"):
        return str(value or "").strip()
    raise ChatHealthyException(
        mode="value_error",
        component="UserParametersTool",
        message=f"{name!r} is not a user parameter. Writable: "
                f"{', '.join(WRITABLE)}.",
    )


class UserParametersTool(ChatHealthyTool):
    TOOL_NAME = "user_parameters"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        params = deps.user_object.userParameters or UserParameters()
        changed: list[str] = []

        if request.verb == "read":
            return Response(parameters=params.model_dump(exclude_none=True))

        if request.verb == "clear_all":
            deps.user_object.userParameters = UserParameters()
            return Response(
                parameters=deps.user_object.userParameters.model_dump(exclude_none=True),
                changed=list(WRITABLE),
            )

        name = (request.name or "").strip()
        if name not in WRITABLE:
            return Response(
                parameters=params.model_dump(exclude_none=True),
                error=f"{name!r} is not a user parameter. Writable: "
                      f"{', '.join(WRITABLE)}.",
            )

        if request.verb == "clear":
            empty = UserParameters().model_dump()[name]
            setattr(params, name, empty)
            changed.append(name)
        else:
            try:
                setattr(params, name, _coerce(name, request.value))
            except ChatHealthyException:
                raise
            except Exception as exc:  # noqa: BLE001 - converted at the boundary
                return Response(
                    parameters=params.model_dump(exclude_none=True),
                    error=f"{name!r} rejected: {exc}",
                )
            changed.append(name)

        deps.user_object.userParameters = params
        return Response(
            parameters=params.model_dump(exclude_none=True),
            changed=changed,
        )


TOOL = UserParametersTool()
