# Move dropped measures to triage or rejected candidates list
import json

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', encoding='utf-8') as f:
    tree = json.load(f)

# Add proximity as CT triage story (enrollment + relevance already there)
for epic in tree['epics']:
    for f in epic.get('features', []):
        if f['feature_id'] == 'EVAL-CT-TRIAGE':
            existing_ids = [s['story_id'] for s in f.get('stories', [])]
            if 'EVAL-CT-TRIAGE-S3' not in existing_ids:
                f['stories'].append({
                    "story_id": "EVAL-CT-TRIAGE-S3",
                    "title": "Geographic Proximity Triage",
                    "description": "Filter out trials that are geographically infeasible. Uses travel calculation from clinical_trials_service.",
                    "parent_feature": "EVAL-CT-TRIAGE",
                    "sprint": 1,
                    "dependencies": [],
                    "size": "S",
                    "evidence": "clinical_trials_service.py has _get_travel_info.",
                    "status": "accepted",
                    "accepted_by": "Claude (moved from dropped measure)",
                    "requirements": []
                })

# Add provider specialty match as provider triage
provider_triage = {
    "feature_id": "EVAL-P-TRIAGE",
    "name": "Provider Triage -- Right Specialty For You",
    "epic_id": "EPIC-1",
    "layer": "backend",
    "capability": "Providers",
    "description": "Pre-qualification: is this provider the right specialty? Uses NUCC taxonomy from SpecialtyService. Unmatched providers excluded from quality scoring.",
    "evidence": "specialty_service.py has dual-pipeline NUCC matching.",
    "priority": "HIGH",
    "status": "accepted",
    "accepted_by": "Claude (moved from dropped measure)",
    "backend_dependencies": [],
    "stories": [{
        "story_id": "EVAL-P-TRIAGE-S1",
        "title": "Specialty Match Triage",
        "description": "Filter providers whose specialty does not match user condition.",
        "parent_feature": "EVAL-P-TRIAGE",
        "sprint": 1, "dependencies": [], "size": "M",
        "evidence": "specialty_service.py", "status": "accepted",
        "accepted_by": "Boss", "requirements": []
    }]
}

provider_triage_ux = {
    "feature_id": "EVAL-P-TRIAGE-UX",
    "name": "Provider Triage Display",
    "epic_id": "EPIC-1",
    "layer": "frontend",
    "capability": "Providers",
    "description": "Show consumer why provider passed/failed triage. Specialty match before quality scores.",
    "evidence": "GAP: No provider triage UI.",
    "priority": "HIGH",
    "status": "accepted",
    "accepted_by": "Boss",
    "backend_dependencies": ["EVAL-P-TRIAGE"],
    "stories": []
}

# Check if already added
existing_fids = set()
for epic in tree['epics']:
    for f in epic.get('features', []):
        existing_fids.add(f['feature_id'])

for epic in tree['epics']:
    if epic['epic_id'] == 'EPIC-1':
        if 'EVAL-P-TRIAGE' not in existing_fids:
            epic['features'].append(provider_triage)
        if 'EVAL-P-TRIAGE-UX' not in existing_fids:
            epic['features'].append(provider_triage_ux)

# Add rejected candidates list -- institutional knowledge
tree['rejected_measure_candidates'] = [
    {
        "id": "EVAL-P-MEASURE-EXPERIENCE",
        "name": "Provider Years in Practice",
        "description": "Years since enumeration or graduation from NPI data",
        "reason_dropped": "Boss reduced to 3 provider measures. Experience is a proxy, not a direct quality signal.",
        "disposition": "rejected",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-P-MEASURE-SPECIALTY",
        "name": "Provider Specialty Match Accuracy",
        "description": "Score how well provider specialty matches user condition using NUCC taxonomy",
        "reason_dropped": "Not a quality measure -- moved to provider triage (EVAL-P-TRIAGE).",
        "disposition": "moved_to_triage",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-P-MEASURE-CREDENTIALS",
        "name": "Provider Credential Completeness",
        "description": "NPI credential completeness -- how many fields populated, license status",
        "reason_dropped": "Overlaps with Board Certification measure. Subsumed by EVAL-P-MEASURE-BOARD.",
        "disposition": "subsumed",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-CT-MEASURE-STATUS",
        "name": "Clinical Trial Enrollment Status",
        "description": "Is the trial actively recruiting",
        "reason_dropped": "Not quality -- moved to triage (EVAL-CT-TRIAGE-S1).",
        "disposition": "moved_to_triage",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-CT-MEASURE-RELEVANCE",
        "name": "Clinical Trial Condition Relevance",
        "description": "Does the trial condition match the user query",
        "reason_dropped": "Not quality -- moved to triage (EVAL-CT-TRIAGE-S2).",
        "disposition": "moved_to_triage",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-CT-MEASURE-PROXIMITY",
        "name": "Clinical Trial Proximity",
        "description": "Distance from user to trial site",
        "reason_dropped": "Logistics not quality -- moved to triage (EVAL-CT-TRIAGE-S3).",
        "disposition": "moved_to_triage",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-CT-MEASURE-APPROVAL",
        "name": "FDA Approval Track Record",
        "description": "Have this sponsors prior trials resulted in FDA approvals",
        "reason_dropped": "Attribute of PI Credibility, not standalone. Folded into EVAL-CT-MEASURE-PI.",
        "disposition": "folded_into_parent",
        "rejected_by": "Boss",
        "date": "2026-04-02"
    },
    {
        "id": "EVAL-P-MEASURE-RATINGS",
        "name": "Provider Patient Ratings Aggregation",
        "description": "Aggregate patient ratings from external sources",
        "reason_dropped": "No reliable public data source. External ratings are biased and gameable.",
        "disposition": "rejected",
        "rejected_by": "GPT (proposed), Boss (rejected)",
        "date": "2026-04-02"
    }
]

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', 'w', encoding='utf-8') as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)

total = sum(len(e.get('features',[])) for e in tree['epics'])
triage_stories = 0
for epic in tree['epics']:
    for f in epic.get('features', []):
        if 'TRIAGE' in f['feature_id']:
            triage_stories += len(f.get('stories', []))

print(f"Total features: {total}")
print(f"Triage stories: {triage_stories}")
print(f"Rejected candidates preserved: {len(tree['rejected_measure_candidates'])}")
for r in tree['rejected_measure_candidates']:
    print(f"  {r['id']}: {r['disposition']} -- {r['reason_dropped'][:60]}")
