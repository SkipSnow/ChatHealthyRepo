# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ClusterTool — wraps ClusterLifecycleManager as a BaseTool.
# Provides: status, wake (reserve), release, force_release.

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
from agent_framework.base_tool import BaseTool, ToolResult

_log = logging.getLogger("ops.cluster_tool")

# Infrastructure error codes that this tool can self-repair
_REPAIRABLE_ERRORS = {"CLUSTER_PAUSED", "CLUSTER_UNREACHABLE", "CONNECTION_TIMEOUT"}


class ClusterTool(BaseTool):
    """Atlas cluster operations: status, wake, release, repair.

    This tool wraps ClusterLifecycleManager. It does NOT know about
    pipeline data, schemas, or business logic.
    """

    def __init__(self, lifecycle_manager):
        self._mgr = lifecycle_manager

    @property
    def name(self) -> str:
        return "cluster"

    @property
    def description(self) -> str:
        return "Manage Atlas cluster lifecycle: status, wake, release, repair."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "wake", "release", "force_release"],
                },
                "cluster_name": {"type": "string"},
                "job_id": {"type": "string"},
                "requester": {"type": "string"},
                "expected_duration_minutes": {"type": "integer"},
                "reservation_class": {
                    "type": "string",
                    "enum": ["automated", "human"],
                },
            },
            "required": ["action"],
        }

    def execute(self, **params) -> ToolResult:
        action = params.get("action")
        cluster_name = params.get("cluster_name", "ChatHealthyDataPipelines")

        try:
            if action == "status":
                data = self._mgr.status(cluster_name)
                return ToolResult(success=True, data=data)

            elif action == "wake":
                data = self._mgr.reserve(
                    cluster_name=cluster_name,
                    job_id=params["job_id"],
                    requester=params["requester"],
                    expected_duration_minutes=params.get("expected_duration_minutes", 120),
                    reservation_class=params.get("reservation_class", "automated"),
                )
                return ToolResult(success=True, data=data)

            elif action == "release":
                data = self._mgr.release(params["job_id"])
                return ToolResult(success=True, data=data)

            elif action == "force_release":
                data = self._mgr.force_release_all(cluster_name)
                return ToolResult(success=True, data=data)

            else:
                return ToolResult(
                    success=False,
                    error_code="UNKNOWN_ACTION",
                    error_message=f"Unknown action: {action}",
                )
        except Exception as e:
            error_code = type(e).__name__
            _log.error("ClusterTool.%s failed: %s", action, e, exc_info=True)
            return ToolResult(
                success=False,
                error_code=error_code,
                error_message=str(e),
                is_infrastructure_error=True,
            )

    def can_handle_error(self, error_code: str) -> bool:
        return error_code in _REPAIRABLE_ERRORS

    def repair(self, error_code: str, context: dict) -> ToolResult:
        """Attempt infra repair: restart cluster."""
        cluster_name = context.get("cluster_name", "ChatHealthyDataPipelines")
        _log.info("Repair attempt: %s on %s", error_code, cluster_name)

        if error_code in {"CLUSTER_PAUSED", "CLUSTER_UNREACHABLE"}:
            try:
                self._mgr._send_wake(cluster_name)
                return ToolResult(success=True, data={"repair_action": "restart_cluster", "cluster": cluster_name})
            except Exception as e:
                return ToolResult(success=False, error_code="REPAIR_FAILED", error_message=str(e),
                                  is_infrastructure_error=True)

        elif error_code == "CONNECTION_TIMEOUT":
            # For connection issues, the wake is the best we can do at infra level
            try:
                self._mgr._send_wake(cluster_name)
                return ToolResult(success=True, data={"repair_action": "reconnect", "cluster": cluster_name})
            except Exception as e:
                return ToolResult(success=False, error_code="REPAIR_FAILED", error_message=str(e),
                                  is_infrastructure_error=True)

        return ToolResult(success=False, error_code="NO_REPAIR",
                          error_message=f"No repair strategy for {error_code}")
