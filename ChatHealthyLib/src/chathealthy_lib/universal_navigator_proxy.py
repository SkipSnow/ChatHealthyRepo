# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The UniversalNavigatorProxy -- the navigator's face.

Per ss_internal_components and flow_tool_to_tool: the Gate reaches the
navigator only through this proxy, and one tool reaches another through it.
The proxy is the navigator's stand-in, in-process or across the boundary
alike. Where the navigator is co-resident it delegates in-process; where the
callee is a satellite the relay is added as that flow is wired onto the proxy.
Triage -- the call type, the may_call graph, the recursion guard -- is read
from the build-generated registry via dispatch_registry.
"""
from __future__ import annotations

from .exceptions import ChatHealthyException


class UniversalNavigatorProxy:
    """The navigator's face. The Gate holds this, never the navigator directly.

    In SharedServices the navigator is co-resident, so a client_sessioned
    arrival the Gate has already proven is a direct in-process hand-off. The
    satellite relay and the tool_to_tool dispatch are wired onto this same
    object as the flow moves onto it, so a caller never learns whether the
    navigator it reached was in this process or across the wire.
    """

    def __init__(self, navigator) -> None:
        self._navigator = navigator

    async def handle_gate(self, gate_req):
        """A client_sessioned arrival the Gate proved. The navigator is
        co-resident here, so this is a direct in-process hand-off."""
        if self._navigator is None:
            raise ChatHealthyException(
                mode="navigator_absent",
                component="universal_navigator_proxy",
                message="the proxy carries no co-resident navigator, so a "
                        "client arrival has nowhere to be routed.")
        return await self._navigator.handle_gate(gate_req)
