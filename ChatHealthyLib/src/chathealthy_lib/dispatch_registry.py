# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The runtime read side of the build-generated composition map.

The build derives one registry from the tools this deploy carries (see
architecture/DevOpsBuildDeployAndEnvironmentManagement/tool_registry_generator.py)
and writes it into the deployed package as `tool_registry.py`, alongside
the ToolRegistry the UtteranceManager already reads. That module carries,
besides the tool contracts:

  * DISPATCH_TABLE — op/route -> the tool(s) subscribing to it. Presence is
    the allowlist: an op no tool subscribes to is one the navigator refuses.
  * MAY_CALL       — tool -> the tools it may call directly (the declared
    tool_to_tool graph).
  * ROUTES         — route_name -> {call_type, proof} for each declared
    external entrance.
  * CALL_TYPES     — the five recognised call types.

This module is the one sanctioned reader every tool AND the navigator use,
so no call site hardcodes `import tool_registry` or invents its own
handling when the registry is absent. It reads the same way the ToolRegistry
is read — the build-generated module on the deployed package's path — and it
does NOT read the live Mongo ToolConfiguration collection: the composition
map is build-generated and deploy-written like ToolRegistry, not written
into a live collection (N34/N20 reserve that; see the refactor plan).

If the module was not generated into the build, every accessor raises. There
is no fallback: a navigator that cannot read what answers an op must fail
loudly, not guess.

This is the READ surface only. It is deliberately NOT yet wired into the
navigator's dispatch or the tools' call sites — that is a later phase. It
exists so those sites can be moved onto it.
"""
from __future__ import annotations

from .exceptions import ChatHealthyException

# The generated module is cached after the first successful import so a hot
# path does not re-import per call. It is a build artifact and never changes
# within a running process.
_generated = None


def _registry():
    """The DispatchRegistry class from the build-generated module.

    Imported lazily because the module does not exist in the source tree —
    it is written per build into the deployed package. Raises if this deploy
    carries no generated registry, with no fallback.
    """
    global _generated
    if _generated is not None:
        return _generated
    try:
        import tool_registry  # build-generated, on the package path at runtime
    except ImportError as exc:
        raise ChatHealthyException(
            mode="dispatch_registry_not_generated",
            component="dispatch_registry",
            message=(
                "the tool registry was not generated into this build, so the "
                "composition map cannot be read. It is written by the build "
                "(tool_registry_generator.write_registry); a runtime without "
                "it is a build that did not run the generator."),
            exception=exc,
        ) from exc
    registry = getattr(tool_registry, "DispatchRegistry", None)
    if registry is None:
        raise ChatHealthyException(
            mode="dispatch_registry_absent_in_module",
            component="dispatch_registry",
            message=(
                "the generated tool_registry module carries no DispatchRegistry; "
                "it was produced by an older generator that emitted only the "
                "tool contracts. Rebuild with the current generator."),
        )
    _generated = registry
    return registry


# ── The dispatch table: what answers an op ─────────────────────────────

def subscribers_for(op: str) -> list[str]:
    """The tool(s) subscribing to one op/route. [] when none do.

    Read by the navigator to dispatch: an op resolves to the tool that
    declared it in its SUBSCRIPTIONS. More than one subscriber is a
    conflict the caller sees and refuses — the registry does not hide it.
    """
    return _registry().subscribers_for(op)


def dispatch_table() -> dict[str, list[str]]:
    """The whole op -> subscribing-tool(s) table."""
    return _registry().dispatch_table()


def ops() -> list[str]:
    """Every op some tool subscribes to (the dispatchable set)."""
    return _registry().ops()


# ── The may_call graph: tool-to-tool ───────────────────────────────────

def may_call_targets(tool: str) -> list[str]:
    """The tools one tool may call directly. [] when it calls none.

    Read by a tool to consult its own declared callees — registry-driven
    tool_to_tool. Raises for a name this build carries no tool for.
    """
    try:
        return _registry().may_call_targets(tool)
    except KeyError as exc:
        raise ChatHealthyException(
            mode="dispatch_registry_unknown_tool",
            component="dispatch_registry",
            message=f"may_call_targets: {tool!r} is not a tool in this build.",
            exception=exc,
        ) from exc


def may_call(caller: str, callee: str) -> bool:
    """Whether caller is declared to be allowed to call callee."""
    return _registry().may_call(caller, callee)


def may_call_graph() -> dict[str, list[str]]:
    """The whole tool -> may-call-targets graph."""
    return _registry().may_call_graph()


# ── The routes: how an external entrance is proven ─────────────────────

def route(route_name: str) -> dict | None:
    """One route's {route_name, call_type, proof}, or None when undeclared."""
    return _registry().route(route_name)


def call_type_of(route_name: str) -> str | None:
    """One route's call_type, or None when the route is not declared."""
    return _registry().call_type_of(route_name)


def routes() -> list[dict]:
    """Every declared route with its call_type and proof."""
    return _registry().routes()


def call_types() -> list[str]:
    """The five recognised call types."""
    return _registry().call_types()
