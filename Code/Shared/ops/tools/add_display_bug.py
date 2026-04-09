"""Add bug: Claude Code markdown can't render repeated table headers."""
import json, os, uuid
BRAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content"))
bug = {
    "id": "BUG-UX-005",
    "type": "low",
    "reason": "Claude Code markdown renderer cannot display repeated table headers mid-table. Headers render as new tables with visual gaps. Limits ability to present long governance reports readably.",
    "resolution_status": "in_analysis",
    "environment": ["local", "dev", "qa", "prod"],
    "date": "2026-04-09",
    "discovery_date": "2026-04-09",
    "due_date": None,
    "next_action": "analysis",
    "status": "open",
    "risk_acceptance_id": None,
    "ch_matrix_id": str(uuid.uuid4()),
}
with open(os.path.join(BRAIN, "bugs.json"), encoding="utf-8") as f:
    data = json.load(f)
data["bugs"].append(bug)
with open(os.path.join(BRAIN, "bugs.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print("BUG-UX-005 added")
