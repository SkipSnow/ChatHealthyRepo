"""Add bug: Claude ignores Boss prompt instructions when other rules authorize action."""
import json, os, uuid
BRAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content"))
bug = {
    "id": "BUG-GOV-002",
    "type": "high",
    "reason": "Claude executed authorized work while Boss had active instruction to make no changes. Boss prompt instructions must take precedence over all other rules including prior authorizations. Claude modified schema.json and bugs.json after Boss said 'don't make any changes to the code base or any of our JSONs' in Normal Mode.",
    "resolution_status": "in_analysis",
    "environment": ["local", "dev", "qa", "prod"],
    "date": "2026-04-09",
    "discovery_date": "2026-04-09",
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
print("BUG-GOV-002 added")
