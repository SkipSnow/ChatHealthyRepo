"""Add show stopper bug BUG-DATA-001 to all environments."""
import json
import os
import uuid

BRAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content"))

bug = {
    "id": "BUG-DATA-001",
    "rule": "SHOW STOPPER: Frontend data insufficient to allow application to work with Delaware data. Missing vector search indexes (provider_vector_index, specialty_vector_index), missing npi unique index, missing state index. Data exists but is not searchable.",
    "severity": "SHOW STOPPER",
    "environments": ["local", "dev", "qa", "prod"],
    "date": "2026-04-09",
    "success_criteria_to_close": "Playwright pytest end-to-end: 'find me a provider who can fix my foot in DE' returns a list of providers. Providers are sent from FindCare to EvaluateCare with authorization ID displayed in the select specialty frame and displayed in the right frame as encrypted (decrypted by the public key of and encrypted by EvaluateCare's private key of the same cert). Handoff must use mutually encrypted TLS 1.2 or greater.",
    "next_action": "analysis",
    "status": "open",
    "ch_matrix_id": str(uuid.uuid4()),
}

with open(os.path.join(BRAIN, "bugs.json"), encoding="utf-8") as f:
    data = json.load(f)

data["bugs"].append(bug)

with open(os.path.join(BRAIN, "bugs.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print("BUG-DATA-001 added to all environments")
