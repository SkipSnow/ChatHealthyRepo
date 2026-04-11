# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BUG-GOV-002: Boss prompt instructions take precedence over all other rules.
#
# Requirement: If Boss constrained Claude from changing state in any way
# in the last 3 hours in the transcript, Claude must respect that constraint.
#
# Success criteria: tool_call returns {allow: False} when Boss constraint
# is active, even if other rules would allow the action.

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Shared", "ops", "tools")))


class TestBossConstraint(unittest.TestCase):
    """BUG-GOV-002: Boss prompt constraint enforcement."""

    def _make_transcript(self, messages):
        """Create a temp transcript JSONL with given messages."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
        f.close()
        return f.name

    def test_boss_says_no_changes_blocks_edit(self):
        """Boss says 'don't make any changes' — Edit must be blocked."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([
            {"role": "user", "content": "don't make any changes to the code base or any of our JSONs"},
        ])
        boot = chathealthy_devops_boot.__new__(chathealthy_devops_boot)
        result = boot._boss_has_active_constraint(transcript)
        self.assertTrue(result["constrained"])
        os.unlink(transcript)

    def test_boss_says_stop_blocks(self):
        """Boss says 'stop' — state changes blocked."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([
            {"role": "user", "content": "stop"},
        ])
        result = chathealthy_devops_boot._boss_has_active_constraint(transcript)
        self.assertTrue(result["constrained"])
        os.unlink(transcript)

    def test_boss_says_do_nothing_blocks(self):
        """Boss says 'do nothing' — state changes blocked."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([
            {"role": "user", "content": "do nothing"},
        ])
        result = chathealthy_devops_boot._boss_has_active_constraint(transcript)
        self.assertTrue(result["constrained"])
        os.unlink(transcript)

    def test_normal_prompt_does_not_block(self):
        """Normal prompt like 'fix the bug' does not trigger constraint."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([
            {"role": "user", "content": "fix the bug in the safety filter"},
        ])
        result = chathealthy_devops_boot._boss_has_active_constraint(transcript)
        self.assertFalse(result["constrained"])
        os.unlink(transcript)

    def test_assistant_message_does_not_trigger(self):
        """Only user/human messages trigger constraint, not assistant."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([
            {"role": "assistant", "content": "I won't make any changes"},
        ])
        result = chathealthy_devops_boot._boss_has_active_constraint(transcript)
        self.assertFalse(result["constrained"])
        os.unlink(transcript)

    def test_empty_transcript_does_not_block(self):
        """Empty transcript — no constraint."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        transcript = self._make_transcript([])
        result = chathealthy_devops_boot._boss_has_active_constraint(transcript)
        self.assertFalse(result["constrained"])
        os.unlink(transcript)

    def test_no_transcript_does_not_block(self):
        """No transcript path — no constraint."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        result = chathealthy_devops_boot._boss_has_active_constraint("")
        self.assertFalse(result["constrained"])

    def test_bug_pydantic_escalates_with_boss_constraint(self):
        """Bug pydantic object returns escalate when Boss constraint active."""
        from chathealthy_devops_boot import bug_governance_constraints
        transcript = self._make_transcript([
            {"role": "user", "content": "don't make any changes"},
        ])
        bug = bug_governance_constraints.from_dict({
            "id": "BUG-TEST-001",
            "type": "high",
            "resolution_status": "in_analysis",
            "risk_acceptance_id": None,
        })
        result = bug.run_governance_process(transcript_path=transcript)
        self.assertFalse(result["comply"])
        self.assertEqual(result["action"], "escalate")
        os.unlink(transcript)


if __name__ == "__main__":
    unittest.main()
