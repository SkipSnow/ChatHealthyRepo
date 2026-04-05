# Clean up epics per Boss directives
import json

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', encoding='utf-8') as f:
    tree = json.load(f)

# Remove MAINT-COST-SCALE, MAINT-BRAIN-DESIGN, MAINT-BRAIN-VELOCITY from EPIC-2
drop_from_epic2 = ['MAINT-COST-SCALE', 'MAINT-BRAIN-DESIGN', 'MAINT-BRAIN-VELOCITY']
for epic in tree['epics']:
    if epic['epic_id'] == 'EPIC-2':
        epic['features'] = [f for f in epic.get('features', []) if f['feature_id'] not in drop_from_epic2]

# Move PIPE-RX-INGEST to EPIC-1 if not already there
pipe_rx = None
for epic in tree['epics']:
    for f in epic.get('features', []):
        if f['feature_id'] == 'PIPE-RX-INGEST':
            pipe_rx = f
            break

# Remove from wherever it is
for epic in tree['epics']:
    epic['features'] = [f for f in epic.get('features', []) if f['feature_id'] != 'PIPE-RX-INGEST']

# Add to EPIC-1 if it existed
if pipe_rx:
    pipe_rx['epic_id'] = 'EPIC-1'
    for epic in tree['epics']:
        if epic['epic_id'] == 'EPIC-1':
            epic['features'].append(pipe_rx)

# Add cost reduction as sprint constraint, not feature
tree['sprint_constraints'] = {
    "cost_reduction": {
        "source": "WO-COST-SCALE-001 (GPT work order)",
        "kpi": "Next month total Mongo + pipeline cost must be lower than this month at 3x data volume",
        "applies_to": "All pipeline features",
        "practices": [
            "Delta processing: skip unchanged records",
            "Auto-termination: pipelines shut down on completion",
            "Batch writes: no chatty per-record Mongo writes",
            "Hot/cold separation: separate access patterns",
            "Default state OFF for non-running pipelines",
            "Max runtime threshold per pipeline"
        ]
    }
}

# Add deferred items to rejected list
if 'deferred_to_post_alpha' not in tree:
    tree['deferred_to_post_alpha'] = []
tree['deferred_to_post_alpha'].extend([
    {"id": "MAINT-BRAIN-DESIGN", "reason": "Brain ships as-is for alpha", "target": "v0.2.1"},
    {"id": "MAINT-BRAIN-VELOCITY", "reason": "Brain ships as-is for alpha", "target": "v0.2.1"},
    {"id": "MAINT-COST-SCALE", "reason": "KPI/constraint, not a deliverable feature", "target": "sprint constraint"}
])

# Clean sprint map
for sprint in tree.get('sprint_map', []):
    if isinstance(sprint, dict) and sprint.get('sprint') == 1:
        shipped = sprint.get('features_shipped', [])
        for d in drop_from_epic2:
            if d in shipped:
                shipped.remove(d)
        if 'PIPE-RX-INGEST' not in shipped:
            shipped.append('PIPE-RX-INGEST')

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', 'w', encoding='utf-8') as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)

for epic in tree['epics']:
    feats = epic.get('features', [])
    print(f"{epic['epic_id']}: {len(feats)} features")
    for f in feats:
        print(f"  {f['feature_id']}: {f['name']}")
print(f"\nDeferred: {len(tree['deferred_to_post_alpha'])}")
print(f"Sprint constraints: cost_reduction KPI applied to all pipeline features")
