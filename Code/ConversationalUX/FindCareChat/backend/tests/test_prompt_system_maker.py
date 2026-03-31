# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Shared"))
from prompt_system_maker import PromptSystemMaker


class TestPromptSystemMaker(unittest.TestCase):

    def setUp(self):
        brain = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "brain")
        self.maker = PromptSystemMaker(brain_dir=brain, env_prefix="dev")

    def test_load_emergency_keywords_returns_list(self):
        keywords = self.maker.load_emergency_keywords()
        self.assertIsInstance(keywords, list)
        self.assertGreater(len(keywords), 10)
        self.assertIn("chest pain", keywords)
        self.assertIn("drowning", keywords)  # F-09 expansion

    def test_load_tool_definitions_returns_list(self):
        tools = self.maker.load_tool_definitions()
        self.assertIsInstance(tools, list)
        self.assertEqual(len(tools), 8)
        names = [t["name"] for t in tools]
        self.assertIn("find_providers", names)
        self.assertIn("search_clinical_trials", names)

    def test_tool_definitions_have_required_fields(self):
        tools = self.maker.load_tool_definitions()
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("input_schema", tool)

    def test_build_system_prompt_contains_rules(self):
        prompt = self.maker.build_system_prompt(emergency_response="CALL 911")
        self.assertIn("RULE 0", prompt)
        self.assertIn("RULE 1", prompt)
        self.assertIn("RULE 6", prompt)
        self.assertIn("CALL 911", prompt)
        self.assertNotIn("RULE 7", prompt)  # no follow-up by default

    def test_build_system_prompt_with_follow_up(self):
        prompt = self.maker.build_system_prompt(emergency_response="", follow_up_check=True)
        self.assertIn("RULE 7", prompt)

    def test_build_welcome_message(self):
        msg = PromptSystemMaker.build_welcome_message()
        self.assertIn("ChatHealthy", msg)
        self.assertIn("Find a doctor", msg)

    def test_trim_short_text(self):
        self.assertEqual(PromptSystemMaker.trim("hello", 100), "hello")

    def test_trim_long_text(self):
        result = PromptSystemMaker.trim("a" * 200, 50)
        self.assertEqual(len(result.split("\n")[0]), 50)
        self.assertIn("truncated", result)

    def test_get_build_number_no_db(self):
        result = PromptSystemMaker.get_build_number(lambda: None, "dev")
        self.assertEqual(result, "?")


if __name__ == "__main__":
    unittest.main()
