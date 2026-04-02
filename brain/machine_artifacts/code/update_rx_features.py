# Update prescription behavior features with two-signal design
import json

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', encoding='utf-8') as f:
    tree = json.load(f)

for epic in tree['epics']:
    for f in epic.get('features', []):
        if f['feature_id'] == 'EVAL-P-RX':
            f['description'] = (
                "AI-enhanced analysis of provider prescription behavior mapped to diseases they treat. "
                "Two signals from CMS Medicare Part D prescriber data: "
                "SIGNAL 1 RELEVANCE: Does the provider prescribing pattern indicate they treat the "
                "patients condition? Maps providers top prescribed drugs to on-label indications via "
                "FDA drug-indication mapping (DailyMed/Orange Book/RxNorm), then maps those indications "
                "to ICD-10 codes. High alignment = high relevance score. "
                "SIGNAL 2 INTEGRITY: Does the prescribing pattern show red flags? Unusual volume of "
                "controlled substances relative to specialty peers, prescribing outside specialty scope, "
                "patterns matching known fraud indicators. Negative signal that degrades score. "
                "We never say fraud to the consumer -- we present the signal and let them decide. "
                "ALPHA GAP: Drug-to-indication mapping may not exist as clean public dataset. "
                "LLM does the mapping if needed (same pattern as SpecialtyService Haiku expansion). "
                "Close in beta with verified mapping table. "
                "PATENT-WORTHY: Nobody combines CMS prescriber data + ICD-10 + drug indication mapping + "
                "LLM synthesis for consumer provider quality signal. Data is public. Method is novel."
            )

        if f['feature_id'] == 'PIPE-RX-INGEST':
            f['description'] = (
                "Azure Function pipeline to ingest CMS Medicare Part D prescriber data. "
                "Step 1: Download CMS Part D Public Use Files from data.cms.gov. "
                "Step 2: Map each row to provider NPI. "
                "Step 3: Map each drug to on-label indications via DailyMed/RxNorm. Fallback: LLM Haiku. "
                "Step 4: Map indications to ICD-10 codes (icd10_loader.py exists). "
                "Step 5: Build prescribing profile per NPI: top drugs, mapped conditions, "
                "controlled substance ratio, specialty alignment score. "
                "Step 6: Store in MongoDB PublicHealthData. "
                "Step 7: Flag anomalies -- controlled substance ratio >2 std dev from specialty mean."
            )

        if f['feature_id'] == 'EVAL-P-RX-UX':
            f['description'] = (
                "Display prescription behavior measure in provider quality score card. "
                "Shows: relevance indicator (provider prescribes for your condition), "
                "top relevant drugs, integrity indicator (clean/flagged), "
                "contribution to overall quality score. GOV-004: present signals, never conclusions."
            )

# List all measures for Boss to pick
print("=== PROVIDER MEASURES ===")
for epic in tree['epics']:
    for f in epic.get('features', []):
        cap = f.get('capability', '')
        fid = f['feature_id']
        if ('MEASURE' in fid or 'RX' in fid) and ('Provider' in cap or 'P-' in fid):
            print(f"  {fid}: {f['name']} [{f.get('priority','')}]")

print("\n=== CLINICAL TRIAL MEASURES ===")
for epic in tree['epics']:
    for f in epic.get('features', []):
        if 'CT-MEASURE' in f['feature_id']:
            print(f"  {f['feature_id']}: {f['name']} [{f.get('priority','')}]")

print("\n=== PIPELINE ===")
for epic in tree['epics']:
    for f in epic.get('features', []):
        if 'PIPE' in f['feature_id']:
            print(f"  {f['feature_id']}: {f['name']}")

print("\n=== SCORING + INFRASTRUCTURE ===")
for epic in tree['epics']:
    for f in epic.get('features', []):
        if f['feature_id'] in ('EVAL-SCORE-PROV', 'EVAL-SCORE-CT', 'EVAL-EXPLAIN', 'EVAL-COMPARE', 'EVAL-WEIGHTS'):
            print(f"  {f['feature_id']}: {f['name']} [{f.get('priority','')}]")

print("\n=== FRONTEND ===")
for epic in tree['epics']:
    for f in epic.get('features', []):
        if f.get('layer') == 'frontend':
            deps = f.get('backend_dependencies', [])
            print(f"  {f['feature_id']}: {f['name']} -> {', '.join(deps)}")

with open('brain/machine_artifacts/.iteration_cache/plan_tree.json', 'w', encoding='utf-8') as f:
    json.dump(tree, f, indent=2, ensure_ascii=False)

print("\nTree updated.")
