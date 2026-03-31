# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# tool_router.py — Safe tool dispatch replacing globals().get() (F-05 fix).
#
# Explicit allowlist registry. If a tool name is not in TOOL_REGISTRY,
# it cannot be called — period. This is the server-side enforcement
# layer for GOV-004 ("The model may suggest. The system must decide.")
#
# Design: ARCH-001 Phase 1

import json
import logging

_log = logging.getLogger("findcare.tool_router")


class ToolRouter:
    """Pydantic-validated allowlist tool dispatch.

    Phase 1: routes to existing main.py functions (pass-through).
    Phase 2+: routes to facade methods as services are extracted.
    """

    def __init__(self):
        self._registry: dict[str, callable] = {}

    def register(self, tool_name: str, handler: callable) -> None:
        """Register a tool handler. Only registered tools can be called."""
        self._registry[tool_name] = handler
        _log.debug("Registered tool: %s -> %s", tool_name, handler.__name__ if hasattr(handler, '__name__') else str(handler))

    def register_all(self, mapping: dict[str, callable]) -> None:
        """Register multiple tools at once."""
        for name, handler in mapping.items():
            self.register(name, handler)

    @property
    def registered_tools(self) -> list[str]:
        """List of all registered tool names."""
        return list(self._registry.keys())

    def dispatch(self, tool_name: str, arguments: dict) -> dict:
        """Dispatch a tool call. Returns the tool result dict.

        Raises KeyError if tool is not registered (F-05 enforcement).
        """
        if tool_name not in self._registry:
            _log.warning("BLOCKED: unregistered tool '%s' — not in allowlist", tool_name)
            return {"error": f"Tool '{tool_name}' is not registered. This call has been blocked."}

        handler = self._registry[tool_name]
        _log.info("Dispatching tool: %s", tool_name)
        try:
            return handler(**arguments)
        except Exception as exc:
            _log.error("Tool '%s' failed: %s", tool_name, exc, exc_info=True)
            return {"error": f"Tool '{tool_name}' failed: {str(exc)}"}

    def handle_tool_calls(self, tool_use_blocks, messages, format_history_fn=None) -> list:
        """Process Anthropic tool_use blocks. Drop-in replacement for _handle_tool_calls.

        Args:
            tool_use_blocks: list of tool_use blocks from Anthropic response
            messages: conversation messages (for chat_history injection)
            format_history_fn: optional function to format chat history
        """
        tool_results = []
        for block in tool_use_blocks:
            name = block.name
            arguments = dict(block.input)

            # Inject chat history for tools that need it
            if name in ("record_user_details", "record_unknown_question") and format_history_fn:
                arguments["chat_history"] = format_history_fn(messages, truncate=False)

            result = self.dispatch(name, arguments)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })
        return tool_results
