# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ChatHealthyTool — abstract base for every tool in the application.

Sits between pydantic-ai's Agent and our concrete tools. The base class
holds abstract functions only; it does NOT pin a runtime, a deps type,
or any shared state. Concrete tools:

  * Declare TOOL_NAME (str), Request (pydantic BaseModel), Response
    (pydantic BaseModel) as class attributes.
  * Override `run(deps, request) -> Response` as an async method.

Some concrete tools wrap a pydantic-ai Agent internally (the LLM-driven
ones, e.g. SpecialtyFilterTool). Others are pure-Python (e.g.
ProviderSearchAndSelectionTool — straight DB query). Both expose the
same `run()` surface so the orchestrator dispatches identically.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Type

from pydantic import BaseModel

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

# Distinguishes "the subclass declared an empty list" from "the subclass
# declared nothing at all". getattr(cls, attr, _UNSET) returns _UNSET only
# when the attribute was never assigned, so silence is caught while a
# legitimate empty SUBSCRIPTIONS/MAY_CALL passes.
_UNSET = object()


class ChatHealthyTool(ABC):
    TOOL_NAME: ClassVar[str]
    Request:   ClassVar[Type[BaseModel]]
    Response:  ClassVar[Type[BaseModel]]

    # ── The registry declarations ─────────────────────────────────────
    # Read out of source at build time by the tool-registry generator's
    # AST walk (never imported, never executed) to compose the one registry
    # the navigator dispatches from. A tool is the single place each of
    # these facts is declared; the build reads them, it does not invent
    # them. See the front_application_application_architecture record.
    #
    #   TOOL_DESCRIPTION  — what the tool is, in one sentence.
    #   SUBSCRIPTIONS     — the gate ops this tool answers (the dispatch
    #                       table). Read from the router's _OP_HANDLERS as
    #                       it stands today: an op whose handler dispatches
    #                       to exactly this tool is its subscription; wire,
    #                       session and multi-tool-orchestration ops stay on
    #                       the UniversalNavigator until Phase 2 makes the
    #                       registry the dispatch authority. A leaf reached
    #                       only by another tool calling it declares [].
    #   MAY_CALL          — the tools this tool may call directly, by
    #                       TOOL.run()/run_and_log(). The declared,
    #                       non-circular call graph. A leaf that calls no
    #                       tool declares [].
    #
    # Exactly one of the two below, never both and never neither:
    #   UTTERANCE_MANAGER_PROMPT — a non-empty routing fragment the
    #                       UtteranceManager utters for this tool. Present
    #                       iff a person's free-text utterance can route to
    #                       the tool (a UtteranceManager target_action).
    #   NOT_UTTERANCE_ROUTABLE = True — no utterance may ever route here
    #                       (wire tools, gesture/op tools, the navigator,
    #                       and safety tools such as the lockout). Reversing
    #                       it is an explicit source change, never an
    #                       omission — silence refuses the subclass.
    TOOL_DESCRIPTION:         ClassVar[str]
    SUBSCRIPTIONS:            ClassVar[list[str]]
    MAY_CALL:                 ClassVar[list[str]]
    UTTERANCE_MANAGER_PROMPT: ClassVar[str]
    NOT_UTTERANCE_ROUTABLE:   ClassVar[bool]

    @abstractmethod
    async def run(self, deps, request):
        """Compute the response from deps + request. Mutate
        `deps.user_object` in place when the tool's work produces session
        state changes; the gate persists the user_object back to
        Users.sessions at the end of the run. Tools do NOT log their own
        invocation — the base class does that uniformly in run_and_log().
        """
        raise NotImplementedError

    async def run_and_log(self, deps, request):
        """Single uniform entry point for tools that are dispatched after
        AuthN. Runs the tool, then appends a tool_invocation entry to the
        user_object's session_conversation_history. Concrete tools never
        have to remember to log themselves — the base class does it.

        Bootstrap tools (e.g. AuthN) whose deps do NOT yet carry a
        user_object should be invoked via `run()` directly, not this
        method.

        A tool that raises is recorded before the exception continues on
        its way. The run used to be outside every guard, so a tool that
        raised appended nothing at all and the action log simply stopped
        -- the one entry that would say why is the one that was never
        written.
        """
        from chathealthy_lib.authentication.agent_deps import append_action
        try:
            args_dump = (
                request.model_dump(exclude_none=True) if request is not None else {}
            )
        except Exception as _exc:
            # Mode 1 (REQ-B-008): args dump for action log only; defaults
            # to {}; action append still happens. log.info + default debug.
            log.info("tool args model_dump failed (using {}): %s", _exc, exc=ChatHealthyException(
                                                                             mode="tool_args_dump_failed",
                                                                             message=f"tool args model_dump failed (using {{}}): {_exc}",
                                                                             component="ChatHealthyTool",
                                                                             exception=_exc,
                                                                         ))
            args_dump = {}

        try:
            result = await self.run(deps, request)
        except BaseException as exc:
            failure: dict = {
                "exception": type(exc).__name__,
                "message": str(exc)[:1000],
            }
            if isinstance(exc, ChatHealthyException):
                failure["mode"] = exc.mode
                failure["component"] = exc.component
                if exc.context:
                    failure["context"] = exc.context
            append_action(
                deps.user_object,
                tool_name=self.TOOL_NAME,
                input_json=args_dump,
                output_json=failure,
            )
            raise

        try:
            result_dump = (
                result.model_dump(exclude_none=True) if result is not None else {}
            )
        except Exception as _exc:
            # Mode 1 (REQ-B-008): result dump for action log only; defaults
            # to {}; action append still happens. log.info + default debug.
            log.info("tool result model_dump failed (using {}): %s", _exc, exc=ChatHealthyException(
                                                                               mode="tool_result_dump_failed",
                                                                               message=f"tool result model_dump failed (using {{}}): {_exc}",
                                                                               component="ChatHealthyTool",
                                                                               exception=_exc,
                                                                           ))
            result_dump = {}
        append_action(
            deps.user_object,
            tool_name=self.TOOL_NAME,
            input_json=args_dump,
            output_json=result_dump,
        )
        return result

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for attr in ("TOOL_NAME", "Request", "Response"):
            if not getattr(cls, attr, None):
                raise ChatHealthyException(
                    mode="chathealthy_tool_subclass_missing_attribute",
                    message=(
                        f"ChatHealthyTool subclass {cls.__name__} missing "
                        f"required class attribute {attr!r}"
                    ),
                    component="ChatHealthyTool",
                )

        description = getattr(cls, "TOOL_DESCRIPTION", None)
        if not isinstance(description, str) or not description.strip():
            raise ChatHealthyException(
                mode="chathealthy_tool_subclass_missing_description",
                message=(
                    f"ChatHealthyTool subclass {cls.__name__} must declare a "
                    f"non-empty TOOL_DESCRIPTION; the registry reads it out of "
                    f"source and cannot describe a tool that describes nothing."
                ),
                component="ChatHealthyTool",
            )

        for attr in ("SUBSCRIPTIONS", "MAY_CALL"):
            value = getattr(cls, attr, _UNSET)
            if value is _UNSET or not isinstance(value, list) \
                    or not all(isinstance(item, str) for item in value):
                raise ChatHealthyException(
                    mode="chathealthy_tool_subclass_bad_registry_list",
                    message=(
                        f"ChatHealthyTool subclass {cls.__name__} must declare "
                        f"{attr} as a list of tool/op names (it may be empty). "
                        f"Silence is not an empty list: the build reads what is "
                        f"declared, so an absent {attr} is an undeclared fact."
                    ),
                    component="ChatHealthyTool",
                    context={"attribute": attr},
                )

        prompt = getattr(cls, "UTTERANCE_MANAGER_PROMPT", None)
        has_prompt = isinstance(prompt, str) and bool(prompt.strip())
        not_routable = getattr(cls, "NOT_UTTERANCE_ROUTABLE", None) is True
        if has_prompt == not_routable:
            raise ChatHealthyException(
                mode="chathealthy_tool_subclass_routability_undeclared",
                message=(
                    f"ChatHealthyTool subclass {cls.__name__} must declare "
                    f"EXACTLY ONE of UTTERANCE_MANAGER_PROMPT (a non-empty "
                    f"routing fragment) or NOT_UTTERANCE_ROUTABLE = True. "
                    f"Declaring neither leaves the build unable to say whether "
                    f"an utterance may reach the tool; declaring both says two "
                    f"contradictory things."
                ),
                component="ChatHealthyTool",
                context={"has_prompt": has_prompt, "not_routable": not_routable},
            )
