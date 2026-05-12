You are analyzing the git history of AI/LLM call sites in ChatHealthy.ai.

Working directory: $CHATHEALTHY_PROJECT_ROOT

For each file below, run: git log --oneline --all -- <file>
Then examine key commits to trace how AI calls evolved.

Files to analyze:
- Code/ConversationalUX/FindCareChat/backend/main.py (classify, chat, safety endpoints)
- Code/Shared/prompt_system_maker.py (system prompt builder)
- Code/ConversationalUX/FindCareChat/backend/domain/find_care/specialty_service.py
- Code/ConversationalUX/FindCareChat/backend/domain/find_care/homeopathic_resolver.py
- Code/ConversationalUX/FindCareChat/backend/domain/find_care/specialty_classifier.py
- Code/ConversationalUX/FindCareChat/backend/domain/shared/safety/safety_service.py
- Code/Shared/llm_client.py
- Code/ConversationalUX/FindCareChat/backend/infrastructure/embeddings/embedding_client.py

For each AI call site found in any version:
1. What model was used
2. What the system prompt said
3. What structured output was expected
4. Whether it still exists in the current code
5. If removed, what replaced it (if anything)

Also search the current codebase for any remaining LLM calls:
  grep -rn "chat.completions\|messages.create\|embeddings.create" Code/

Write your output as valid JSON to: _oneshots/test_output/lineage/02_prompt_history.json

Schema:
{
  "generated_at": "ISO timestamp",
  "files_analyzed": ["list"],
  "ai_calls": [
    {
      "name": "descriptive name",
      "file": "which file",
      "model": "model name",
      "status": "active|replaced|removed",
      "replaced_by": "what replaced it or null",
      "capability_gap": "none|partial|full",
      "gap_description": "what is missing",
      "system_prompt_summary": "one paragraph",
      "documented": true or false,
      "backlog_ref": "requirement ID or null"
    }
  ]
}
