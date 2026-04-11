# Add maintenance features per Boss directive
import json

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', encoding='utf-8') as f:
    tree = json.load(f)

new_features = [
    {
        "feature_id": "MAINT-COUNTY-ENRICH",
        "name": "Provider County Economic Enrichment",
        "epic_id": "EPIC-2",
        "layer": "backend",
        "capability": "DataPipelines",
        "description": "Enrich provider records with county-level economic data: GDP, mean income, average income, rural vs urban flag. Uses existing county enrichment pipeline workflow. Data source: Census Bureau ACS or BEA county-level economic data.",
        "evidence": "Code/DataPipelines/county_enrichment_job.py (6-pass county enrichment exists). ZIP-county crosswalk already loaded.",
        "priority": "HIGH",
        "status": "accepted",
        "accepted_by": "Boss (direct mandate)",
        "backend_dependencies": [],
        "stories": []
    },
    {
        "feature_id": "MAINT-E2E-PIPELINE",
        "name": "End-to-End Pipeline Run with Embedding",
        "epic_id": "EPIC-2",
        "layer": "backend",
        "capability": "DataPipelines",
        "description": "Full end-to-end provider pipeline: load, enrich, embed. Must use current workflow (FindCarePipeline orchestrator). Single trigger runs the complete chain through to vector embeddings in the frontend cluster.",
        "evidence": "Code/DataPipelines/provider_load_manager.py (8-step durable workflow). Code/DataPipelines/embedding_worker.py. Code/DataPipelines/copy_to_frontend.py.",
        "priority": "HIGH",
        "status": "accepted",
        "accepted_by": "Boss (direct mandate)",
        "backend_dependencies": [],
        "stories": []
    },
    {
        "feature_id": "MAINT-INCREMENTAL-LOAD",
        "name": "Incremental Provider Load via Array",
        "epic_id": "EPIC-2",
        "layer": "backend",
        "capability": "DataPipelines",
        "description": "Add incremental provider loading. Accept an array of provider NPIs. Process only those providers through the full pipeline (load, enrich, embed). Does not require a full state reload.",
        "evidence": "Code/DataPipelines/provider_load_manager.py (current pipeline is full-state). GAP: No incremental path exists.",
        "priority": "HIGH",
        "status": "accepted",
        "accepted_by": "Boss (direct mandate)",
        "backend_dependencies": ["MAINT-E2E-PIPELINE"],
        "stories": []
    },
    {
        "feature_id": "MAINT-SPECIALTY-FILTER",
        "name": "Specialty Code Filter for Pipeline",
        "epic_id": "EPIC-2",
        "layer": "backend",
        "capability": "DataPipelines",
        "description": "Add specialty code include/exclude filter to the pipeline. Filter providers by NUCC taxonomy code before processing. Supports both inclusion (process only these specialties) and exclusion (skip these specialties). Reduces processing volume and cost.",
        "evidence": "Code/DataPipelines/provider_load_manager.py. Code/Shared/ops/uat_report.py (NUCC codes in SpecialtyMetaData collection).",
        "priority": "HIGH",
        "status": "accepted",
        "accepted_by": "Boss (direct mandate)",
        "backend_dependencies": [],
        "stories": []
    }
]

# Replace EPIC-2 entirely with Boss's directive
for epic in tree['epics']:
    if epic['epic_id'] == 'EPIC-2':
        epic['features'] = new_features

# Add to Sprint 1
for sprint in tree.get('sprint_map', []):
    if isinstance(sprint, dict) and sprint.get('sprint') == 1:
        shipped = sprint.get('features_shipped', [])
        for f in new_features:
            if f['feature_id'] not in shipped:
                shipped.append(f['feature_id'])

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', 'w', encoding='utf-8') as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)

epic2_count = sum(len(e.get('features', [])) for e in tree['epics'] if e['epic_id'] == 'EPIC-2')
print(f"EPIC-2 now has {epic2_count} features")
for f in new_features:
    print(f"  {f['feature_id']}: {f['name']}")
