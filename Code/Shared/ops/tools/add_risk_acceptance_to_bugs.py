"""Add risk_acceptance_id = null to every bug record."""
import json, os
BRAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content"))
with open(os.path.join(BRAIN, "bugs.json"), encoding="utf-8") as f:
    data = json.load(f)
count = 0
for bug in data.get("bugs", []):
    if "risk_acceptance_id" not in bug:
        bug["risk_acceptance_id"] = None
        count += 1
with open(os.path.join(BRAIN, "bugs.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print(f"Added risk_acceptance_id=null to {count} bugs")
