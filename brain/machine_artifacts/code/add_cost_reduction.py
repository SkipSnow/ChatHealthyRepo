# Add GPT's cost reduction work order to Sprint 1
import json

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', encoding='utf-8') as f:
    tree = json.load(f)

cost_feature = {
    "feature_id": "MAINT-COST-SCALE",
    "name": "Reduce Mongo and Pipeline Cost While Tripling Data Volume",
    "epic_id": "EPIC-2",
    "layer": "backend",
    "capability": "DataPipelines",
    "description": "Sub-linear cost scaling: next month total cost must be lower while data volume grows 3x. Delta processing, idempotency, auto-termination, batch writes, hot/cold separation.",
    "evidence": "Code/DataPipelines/ (existing pipeline). WO-COST-SCALE-001 from GPT.",
    "priority": "CRITICAL",
    "status": "accepted",
    "accepted_by": "Boss (critical priority)",
    "backend_dependencies": [],
    "stories": [
        {
            "story_id": "COST-S1",
            "title": "Delta Processing -- Skip Unchanged Records",
            "description": "Process only new or changed records deterministically.",
            "parent_feature": "MAINT-COST-SCALE",
            "sprint": 1, "dependencies": [], "size": "L",
            "evidence": "provider_load_manager.py, embedding_worker.py",
            "status": "accepted", "accepted_by": "Boss",
            "requirements": [
                {"req_id": "WO-R001", "story_id": "COST-S1", "requirement": "The system must process only new or changed records and must skip unchanged records deterministically.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R002", "story_id": "COST-S1", "requirement": "The system must prevent re-embedding, re-enrichment, or re-ingestion of records already processed successfully for the active version unless explicitly invalidated.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R006", "story_id": "COST-S1", "requirement": "The implementation must support safe restart after termination without duplicating successfully completed work.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""}
            ]
        },
        {
            "story_id": "COST-S2",
            "title": "Auto-Termination and Shutdown Controls",
            "description": "Every paid pipeline terminates automatically. Default state OFF. Max runtime enforced.",
            "parent_feature": "MAINT-COST-SCALE",
            "sprint": 1, "dependencies": [], "size": "M",
            "evidence": "atlas_cluster_manager.py, function_app.py",
            "status": "accepted", "accepted_by": "Boss",
            "requirements": [
                {"req_id": "WO-R003", "story_id": "COST-S2", "requirement": "Every paid pipeline execution path must terminate resources automatically on success, controlled failure, and timeout.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R004", "story_id": "COST-S2", "requirement": "The default operational state for non-running pipelines must be OFF or idle, never implicitly active.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R005", "story_id": "COST-S2", "requirement": "Each pipeline must have a maximum runtime threshold after which execution is terminated or scaled down automatically.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R011", "story_id": "COST-S2", "requirement": "The system must log pipeline start, completion, termination reason, and resource release confirmation for each run.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R013", "story_id": "COST-S2", "requirement": "The system should expose a simple operator-visible run state such as RUNNING, IDLE, FAILED, TERMINATED.", "source": "WO-COST-SCALE-001", "priority": "should-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R014", "story_id": "COST-S2", "requirement": "The system should provide a manual kill switch that stops all active pipeline work safely.", "source": "WO-COST-SCALE-001", "priority": "should-have", "status": "accepted", "label": ""}
            ]
        },
        {
            "story_id": "COST-S3",
            "title": "Batch Writes and Hot/Cold Separation",
            "description": "Bulk Mongo writes. Separate hot from cold data. Defer expensive processing to on-demand.",
            "parent_feature": "MAINT-COST-SCALE",
            "sprint": 1, "dependencies": [], "size": "M",
            "evidence": "provider_worker.py, copy_to_frontend.py",
            "status": "accepted", "accepted_by": "Boss",
            "requirements": [
                {"req_id": "WO-R007", "story_id": "COST-S3", "requirement": "Mongo write patterns must use bulk or batch operations wherever feasible and must avoid chatty per-record write behavior.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R008", "story_id": "COST-S3", "requirement": "The solution must identify and separate hot data access paths from cold or reference data paths where this reduces cost.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R012", "story_id": "COST-S3", "requirement": "Expensive processing should be deferred to on-demand execution where precomputation is not required for core system behavior.", "source": "WO-COST-SCALE-001", "priority": "should-have", "status": "accepted", "label": ""}
            ]
        },
        {
            "story_id": "COST-S4",
            "title": "Cost Model and Documentation",
            "description": "Before-and-after cost model. Document work elimination. Prove cost reduction at 3x.",
            "parent_feature": "MAINT-COST-SCALE",
            "sprint": 1, "dependencies": ["COST-S1", "COST-S2", "COST-S3"], "size": "M",
            "evidence": "WO-COST-SCALE-001 deliverables D1, D4",
            "status": "accepted", "accepted_by": "Boss",
            "requirements": [
                {"req_id": "WO-R009", "story_id": "COST-S4", "requirement": "The implementation must document each step in the pipeline where work is eliminated, deferred, or batched.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""},
                {"req_id": "WO-R010", "story_id": "COST-S4", "requirement": "The system must produce a before-and-after cost model showing expected monthly cost behavior at current scale and at approximately 3x data scale.", "source": "WO-COST-SCALE-001", "priority": "must-have", "status": "accepted", "label": ""}
            ]
        }
    ]
}

for epic in tree['epics']:
    if epic['epic_id'] == 'EPIC-2':
        epic['features'].append(cost_feature)

for sprint in tree.get('sprint_map', []):
    if isinstance(sprint, dict) and sprint.get('sprint') == 1:
        shipped = sprint.get('features_shipped', [])
        if 'MAINT-COST-SCALE' not in shipped:
            shipped.append('MAINT-COST-SCALE')
        sprint['sprint_goal'] = "PI Planning, Design, Implementation of Evaluate Care, Cost Reduction Pipeline, Smoke Test (DE only)"

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', 'w', encoding='utf-8') as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)

total_f = sum(len(e.get('features', [])) for e in tree['epics'])
total_s = sum(len(f.get('stories', [])) for e in tree['epics'] for f in e.get('features', []))
total_r = sum(len(s.get('requirements', [])) for e in tree['epics'] for f in e.get('features', []) for s in f.get('stories', []))
print(f"Features: {total_f} Stories: {total_s} Requirements: {total_r}")
print("MAINT-COST-SCALE: 4 stories, 14 requirements, Sprint 1, CRITICAL")
