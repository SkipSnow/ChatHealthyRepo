# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Unit tests for ToolRouter (F-05 fix)
# Verifies: allowlist enforcement, dispatch, unregistered tool blocking

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from application.tool_router import ToolRouter


def _mock_tool(**kwargs):
    return {"result": "ok", "args": kwargs}


def _failing_tool(**kwargs):
    raise ValueError("intentional failure")


class TestToolRouter(unittest.TestCase):

    def setUp(self):
        self.router = ToolRouter()
        self.router.register("mock_tool", _mock_tool)
        self.router.register("failing_tool", _failing_tool)

    def test_registered_tool_dispatches(self):
        result = self.router.dispatch("mock_tool", {"key": "value"})
        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["args"]["key"], "value")

    def test_unregistered_tool_blocked(self):
        """F-05: unregistered tools must be blocked."""
        result = self.router.dispatch("exec", {})
        self.assertIn("error", result)
        self.assertIn("not registered", result["error"])

    def test_globals_bypass_blocked(self):
        """F-05: verify that Python builtins cannot be called."""
        for dangerous in ["exec", "eval", "exit", "__import__", "compile", "open"]:
            result = self.router.dispatch(dangerous, {})
            self.assertIn("error", result, f"{dangerous} should be blocked")

    def test_registered_tools_list(self):
        self.assertIn("mock_tool", self.router.registered_tools)
        self.assertIn("failing_tool", self.router.registered_tools)
        self.assertEqual(len(self.router.registered_tools), 2)

    def test_failing_tool_returns_error(self):
        result = self.router.dispatch("failing_tool", {})
        self.assertIn("error", result)
        self.assertIn("intentional failure", result["error"])

    def test_register_all(self):
        r = ToolRouter()
        r.register_all({"a": _mock_tool, "b": _mock_tool})
        self.assertEqual(len(r.registered_tools), 2)

    def test_empty_arguments(self):
        result = self.router.dispatch("mock_tool", {})
        self.assertEqual(result["result"], "ok")


if __name__ == "__main__":
    unittest.main()
