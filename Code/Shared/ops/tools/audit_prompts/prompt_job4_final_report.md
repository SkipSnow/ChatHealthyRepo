You are producing the final audit report for ChatHealthy.ai codebase history.

Working directory: c:\chatHealthy\findCare

Read these checkpoint files:
- test_output/lineage/00_brain_intent.json
- test_output/lineage/01_gui_history.json
- test_output/lineage/02_prompt_history.json
- test_output/lineage/03_cross_reference.json

Produce a final report with:

1. METHODOLOGY: Describe how this audit was conducted (deterministic orchestrator,
   disposable AI workers, git history analysis, brain artifact cross-reference)

2. ASSUMPTIONS: List what was assumed (e.g. brain is source of intent, git is
   source of build history, current code is source of reality)

3. NUMBERED FINDINGS: Most important first (lowest number), least important last.
   Each finding MUST include:
   - Finding number
   - Title
   - Category (undocumented_loss | documented_unplanned | intentional_replacement)
   - Priority (critical | high | medium | low)
   - Evidence from codebase (file, commit, line range — or "no code exists")
   - Evidence from brain (artifact reference, requirement ID, or best practice)
   - Description of the gap

4. RECOMMENDATIONS: For each finding, recommend:
   - Action (restore from git | re-implement | merge into prompt manufacturer | accept risk)
   - Effort estimate (S | M | L)
   - Dependencies
   - Suggested priority for recovery

5. SUMMARY: Total capabilities intended, currently active, lost undocumented,
   lost documented, intentionally replaced. Token cost of this audit.

Write your output as valid JSON to: test_output/lineage/04_final_report.json

Schema:
{
  "generated_at": "ISO timestamp",
  "methodology": "text",
  "assumptions": ["list of assumptions"],
  "summary": {
    "total_intended": number,
    "currently_active": number,
    "lost_undocumented": number,
    "lost_documented": number,
    "intentionally_replaced": number
  },
  "findings": [
    {
      "number": 1,
      "title": "finding title",
      "category": "undocumented_loss|documented_unplanned|intentional_replacement",
      "priority": "critical|high|medium|low",
      "code_evidence": "file:line or commit hash or 'no code exists'",
      "brain_evidence": "brain file reference or requirement ID",
      "description": "what the gap is",
      "recommendation": {
        "action": "restore|re-implement|prompt_manufacturer|accept_risk",
        "effort": "S|M|L",
        "dependencies": [],
        "recovery_priority": number
      }
    }
  ]
}
