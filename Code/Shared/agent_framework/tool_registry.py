# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ToolRegistry — allowlist of tools an agent can use.
# Same pattern as ToolRouter in the chat app (GOV-004).
# Agent can only call registered tools.

import logging
from typing import Optional
from .base_tool import BaseTool, ToolResult

_log = logging.getLogger("agent.tool_registry")


class ToolRegistry:
    """Allowlist registry for agent tools.

    GOV-004: The model may suggest. The system must decide.
    The agent suggests a tool. The registry decides if it's allowed.
    """

    def __init__(self, agent_name: str):
        self._agent = agent_name
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Only registered tools can be called."""
        self._tools[tool.name] = tool
        _log.info("[%s] Registered tool: %s — %s", self._agent, tool.name, tool.description)

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name. Returns None if not registered."""
        return self._tools.get(name)

    def execute(self, tool_name: str, **params) -> ToolResult:
        """Execute a registered tool. Rejects unregistered tools."""
        tool = self._tools.get(tool_name)
        if not tool:
            _log.warning("[%s] BLOCKED: unregistered tool '%s'", self._agent, tool_name)
            return ToolResult(success=False, error_code="TOOL_NOT_REGISTERED",
                              error_message=f"Tool '{tool_name}' is not registered for agent '{self._agent}'")

        _log.info("[%s] Executing: %s", self._agent, tool_name)
        try:
            return tool.execute(**params)
        except Exception as e:
            _log.error("[%s] Tool '%s' failed: %s", self._agent, tool_name, e, exc_info=True)
            return ToolResult(success=False, error_code="TOOL_EXCEPTION",
                              error_message=str(e))

    def find_repair_tool(self, error_code: str) -> Optional[BaseTool]:
        """Find a tool that can repair the given error. Returns None if no tool can."""
        for tool in self._tools.values():
            if tool.can_handle_error(error_code):
                return tool
        return None
