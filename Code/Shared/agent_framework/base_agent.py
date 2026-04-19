# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BaseAgent — the brain. Has tools. Receives events. Decides. Acts.
# v0.1: rule-based decisions. v0.2: AI-powered triage.

import logging
from abc import ABC, abstractmethod
from .tool_registry import ToolRegistry
from .base_tool import BaseTool, ToolResult

_log = logging.getLogger("agent.base")


class BaseAgent(ABC):
    """Base class for all ChatHealthy AI agents.

    An agent:
    - Has a tool registry (what it can do)
    - Receives events (what happened)
    - Decides (rule-based v0.1, AI v0.2)
    - Acts (calls a tool or escalates)

    Subclasses implement: handle_event(), register_tools()
    """

    def __init__(self, name: str, push_fn=None):
        self.name = name
        self.tools = ToolRegistry(agent_name=name)
        self._push = push_fn  # Pushover alert to human
        self.register_tools()

    @abstractmethod
    def register_tools(self) -> None:
        """Register all tools this agent can use."""
        ...

    @abstractmethod
    def handle_event(self, event: dict) -> ToolResult:
        """Receive an event and decide what to do.

        Events come from: timer, API call, error callback, heartbeat.
        The agent reads the event, picks a tool (or escalates), and acts.
        """
        ...

    def escalate_to_boss(self, title: str, message: str):
        """Alert human via Pushover."""
        _log.warning("[%s] ESCALATE: %s — %s", self.name, title, message)
        if self._push:
            self._push(title, message)

    def escalate_to_dev(self, title: str, message: str):
        """Escalate to Dev Manager (Claude). For now, same as human alert."""
        _log.warning("[%s] DEV ESCALATE: %s — %s", self.name, title, message)
        if self._push:
            self._push(f"[Dev] {title}", message)

    def attempt_self_repair(self, error_code: str, context: dict) -> ToolResult:
        """Try to find a tool that can repair the error. If none, escalate."""
        tool = self.tools.find_repair_tool(error_code)
        if tool:
            _log.info("[%s] Self-repair: %s using %s", self.name, error_code, tool.name)
            return tool.repair(error_code, context)
        _log.info("[%s] No repair tool for %s — escalating", self.name, error_code)
        self.escalate_to_boss("Self-repair failed", f"No tool can handle error: {error_code}")
        return ToolResult(success=False, error_code="NO_REPAIR_TOOL",
                          error_message=f"No tool registered to repair {error_code}")
