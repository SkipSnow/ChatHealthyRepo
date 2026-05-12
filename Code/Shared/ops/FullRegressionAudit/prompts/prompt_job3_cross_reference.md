You are producing a cross-reference gap analysis for ChatHealthy.ai.

Working directory: $CHATHEALTHY_PROJECT_ROOT

Read these checkpoint files from previous jobs:
- _oneshots/test_output/lineage/00_brain_intent.json (what was intended)
- _oneshots/test_output/lineage/01_gui_history.json (what GUI lost)
- _oneshots/test_output/lineage/02_prompt_history.json (what AI calls lost)

Also read:
- brain/machine_artifacts/content/bugs.json (search with grep for relevant bug IDs)
- brain/machine_artifacts/content/agile_backlog.json (search with grep, do NOT read whole file)

For each capability in 00_brain_intent.json:
1. Is it in the current code? (check 01 and 02 for active capabilities)
2. Was it lost? (check 01 and 02 for lost capabilities)
3. Is the loss documented in bugs.json or agile_backlog.json?

Categorize each finding as:
- UNDOCUMENTED LOSS: lost with no bug or backlog entry
- DOCUMENTED UNPLANNED: tracked as bug but no story to restore
- INTENTIONAL REPLACEMENT: deliberately replaced (e.g. vector search for classify)

Write your output as valid JSON to: _oneshots/test_output/lineage/03_cross_reference.json

Schema:
{
  "generated_at": "ISO timestamp",
  "undocumented_losses": [
    {"capability": "name", "evidence_from": "which checkpoint", "brain_reference": "which brain file", "priority": "critical|high|medium|low"}
  ],
  "documented_unplanned": [
    {"capability": "name", "bug_or_req_id": "ID", "status": "open|closed", "priority": "critical|high|medium|low"}
  ],
  "intentional_replacements": [
    {"old": "what was replaced", "new": "what replaced it", "gap": "none|partial|full", "notes": "details"}
  ]
}
