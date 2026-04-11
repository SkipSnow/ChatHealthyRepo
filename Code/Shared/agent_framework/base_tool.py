# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BaseTool — interface every agent tool must implement.
# Tools are dumb. They execute. The agent decides when and why.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Standard result from any tool execution."""
    success: bool
    data: Any = None
    error_code: str = ""
    error_message: str = ""
    is_infrastructure_error: bool = False  # Ops Manager handles these
    is_pipeline_error: bool = False        # Dev Manager handles these

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "data": self.data,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "is_infrastructure_error": self.is_infrastructure_error,
            "is_pipeline_error": self.is_pipeline_error,
        }


class BaseTool(ABC):
    """Every agent tool implements this interface.

    Tools don't decide. They execute and report.
    The agent reads the result and decides what to do.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name. Used in tool registry."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description. Agent reads this to decide which tool to use."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON Schema for tool parameters. Agent uses this to call the tool."""
        ...

    @abstractmethod
    def execute(self, **params) -> ToolResult:
        """Execute the tool. Returns success/failure with data or error."""
        ...

    def can_handle_error(self, error_code: str) -> bool:
        """Can this tool repair the given error? Default: no."""
        return False

    def repair(self, error_code: str, context: dict) -> ToolResult:
        """Attempt self-repair for the given error. Default: not implemented."""
        return ToolResult(success=False, error_code="NOT_IMPLEMENTED",
                          error_message=f"{self.name} cannot repair {error_code}")
