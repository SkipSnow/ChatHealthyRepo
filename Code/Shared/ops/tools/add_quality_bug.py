"""Add show stopper bug BUG-PIPE-010."""
import json, os, uuid
BRAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content"))
bug = {
    "id": "BUG-PIPE-010",
    "rule": "SHOW STOPPER: Quality pipeline ships provider_quality to frontend without embeddings. 25,591 DE records have zero prescriber_embedding on both pipeline and frontend clusters. The embedding step (Step 6) in prescriber_pipeline_manager.py either never ran or failed silently for DE. Pipeline must not copy provider_quality to frontend until embeddings are complete. v4-025 violation: pipeline is not idempotent — partial data shipped as complete.",
    "severity": "SHOW STOPPER",
    "environments": ["local", "dev", "qa", "prod"],
    "date": "2026-04-09",
    "discovery_date": "2026-04-09",
    "next_action": "analysis",
    "status": "open",
    "ch_matrix_id": str(uuid.uuid4()),
}
with open(os.path.join(BRAIN, "bugs.json"), encoding="utf-8") as f:
    data = json.load(f)
data["bugs"].append(bug)
with open(os.path.join(BRAIN, "bugs.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print("BUG-PIPE-010 added to all environments")
