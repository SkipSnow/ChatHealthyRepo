You are analyzing the git history of ChatHealthy.ai GUI files to identify lost capabilities.

Working directory: $CHATHEALTHY_PROJECT_ROOT

For each of these files, run: git log --oneline --all -- <file>
Then for significant commits (especially ones that add or remove functions/features), run: git show <commit>:<file>
Compare what existed in older versions vs what exists now.

Files to analyze:
- Code/ConversationalUX/FindCareChat/frontend/src/components/FindCareApp.tsx
- Code/ConversationalUX/FindCareChat/frontend/src/components/ChatWindow.tsx
- Code/ConversationalUX/FindCareChat/frontend/src/components/GUIManager.tsx
- Code/ConversationalUX/FindCareChat/frontend/src/App.tsx
- Website/index.html
- Code/ConversationalUX/FindCareChat/backend/main.py (focus on route definitions and endpoints)

For each file:
1. Get the commit list
2. Compare the earliest substantial version with the current version
3. Identify functions, routes, UI elements that were added then removed
4. Note any features that were commented out

Also check: which component is actually mounted? Is ChatWindow used or is FindCareApp used? Are there dead components?

Write your output as valid JSON to: _oneshots/test_output/lineage/01_gui_history.json

Schema:
{
  "generated_at": "ISO timestamp",
  "files_analyzed": ["list"],
  "commits_scanned": number,
  "lost_capabilities": [
    {
      "capability": "name",
      "file": "which file",
      "added_commit": "hash",
      "removed_commit": "hash",
      "what_it_did": "description",
      "code_evidence": "key function names or JSX",
      "documented": true or false,
      "backlog_ref": "requirement ID or null"
    }
  ],
  "active_capabilities": [
    {"capability": "name", "file": "which file"}
  ],
  "dead_components": [
    {"component": "name", "file": "path", "reason": "why it is dead"}
  ]
}
