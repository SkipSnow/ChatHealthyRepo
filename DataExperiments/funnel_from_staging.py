"""Funnel analysis from STAGING, not from re-downloaded source.

Reads PublicStaging.StagingMedicarePartD_v_1 and PublicStaging.StagingOpenFdaLabels_v_1
on the pipeline cluster. Wakes the cluster first and waits for IDLE; does not
pause it afterwards (the reaper owns that).

Everything heavy is a server-side aggregation. Nothing is written to any collection.
"""
import json
import os
import sys
import time
import urllib.request
from base64 import b64encode

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

from dotenv import load_dotenv  # noqa: E402

load_dotenv(r"C:\CHATHealthyLLC\.env")

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (  # noqa: E402
    brand_join_key, molecule_canonical)

LOG = r"C:\CHATHealthyLLC\_oneshots\funnel_progress.txt"
OUT = r"C:\CHATHealthyLLC\_oneshots\funnel_result.json"
CLUSTER = "ChatHealthyDataPipelines"


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log.info("%s", line)


def _atlas(method, path, body=None):
    """Atlas Admin API with digest auth, using the pipeline key pair."""
    import urllib.error
    pub = os.environ["ATLAS_PIPELINE_PUBLIC_KEY"]
    prv = os.environ["ATLAS_PIPELINE_PRIVATE_KEY"]
    proj = os.environ["ATLAS_PROJECT_ID"]
    url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{proj}{path}"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, "https://cloud.mongodb.com", pub, prv)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.atlas.2024-08-05+json")
    if data:
        req.add_header("Content-Type", "application/vnd.atlas.2024-08-05+json")
    with opener.open(req, timeout=180) as r:
        return json.loads(r.read())


def wake():
    state = _atlas("GET", f"/clusters/{CLUSTER}")
    note(f"cluster state={state.get('stateName')} paused={state.get('paused')} "
         f"tier={state['replicationSpecs'][0]['regionConfigs'][0]['electableSpecs']['instanceSize']}")
    if state.get("paused"):
        note("dispatching resume")
        _atlas("PATCH", f"/clusters/{CLUSTER}", {"paused": False})
    waited = 0
    while True:
        state = _atlas("GET", f"/clusters/{CLUSTER}")
        if state.get("stateName") == "IDLE" and not state.get("paused"):
            note(f"cluster IDLE and unpaused after {waited}s")
            return
        note(f"waiting: state={state.get('stateName')} paused={state.get('paused')} ({waited}s)")
        time.sleep(20)
        waited += 20


def main():
    for p in (LOG, OUT):
        if os.path.exists(p):
            os.remove(p)
    wake()

    client = ChatHealthyMongoUtilities().getConnection("claudeCodeAgent",
                                                       "ChatHealthyDataPipelines")
    stg = client["PublicStaging"]
    names = stg.list_collection_names()
    note("staging collections: " + ", ".join(sorted(names)))

    partd = next((n for n in names if "MedicarePartD" in n), None)
    fda = next((n for n in names if "OpenFdaLabels" in n), None)
    note(f"using partd={partd} fda={fda}")

    result = {"partd_collection": partd, "fda_collection": fda}

    note("counting Part D rows (server-side)")
    result["partd_rows"] = stg[partd].estimated_document_count()
    note(f"  rows={result['partd_rows']:,}")

    note("collapsing Part D to distinct (brand, generic) pairs with volume")
    pairs = list(stg[partd].aggregate([
        {"$group": {
            "_id": {"b": "$raw.Brnd_Name", "g": "$raw.Gnrc_Name"},
            "rows": {"$sum": 1},
            "clms": {"$sum": {"$toDouble": {"$ifNull": ["$raw.Tot_Clms", 0]}}},
            "cost": {"$sum": {"$toDouble": {"$ifNull": ["$raw.Tot_Drug_Cst", 0]}}},
        }},
    ], allowDiskUse=True, maxTimeMS=1800000))
    note(f"  distinct pairs={len(pairs):,}")

    brands = {}
    generics = {}
    for p in pairs:
        b = (p["_id"].get("b") or "").strip()
        g = (p["_id"].get("g") or "").strip()
        brands.setdefault(b, {"clms": 0.0, "generics": set()})
        brands[b]["clms"] += p["clms"]
        brands[b]["generics"].add(g)
        generics.setdefault(g, set()).add(b)
    result["distinct_pairs"] = len(pairs)
    result["distinct_brands"] = len(brands)
    result["distinct_generics"] = len(generics)
    result["generics_mapping_many_brands"] = sum(1 for v in generics.values() if len(v) > 1)
    result["brands_mapping_many_generics"] = sum(1 for v in brands.values() if len(v["generics"]) > 1)
    result["total_claims"] = sum(v["clms"] for v in brands.values())
    note(f"  brands={result['distinct_brands']:,} generics={result['distinct_generics']:,} "
         f"claims={result['total_claims']:,.0f}")

    note("building the FDA name universe from staging (server-side projection)")
    fda_brand, fda_generic, fda_substance = {}, {}, {}
    scanned = in_scope = 0
    from pipeline.MedicareProviderEvaluationDataPipeline.steps.index_fda_prescription_records import (
        _has_indication, _otc_basis)
    cur = stg[fda].find({}, {"raw": 1}).batch_size(500)
    for doc in cur:
        raw = doc.get("raw") or {}
        scanned += 1
        if scanned % 25000 == 0:
            note(f"  fda scanned={scanned:,} in_scope={in_scope:,}")
        if not _has_indication(raw):
            continue
        if _otc_basis(raw) is not None:
            continue
        in_scope += 1
        sid = raw.get("set_id")
        ofda = raw.get("openfda") or {}
        for n in (ofda.get("brand_name") or []):
            if n:
                fda_brand.setdefault(str(n).strip().upper(), set()).add(sid)
                k = brand_join_key(n)
                if k:
                    fda_brand.setdefault("NORM::" + k, set()).add(sid)
        for n in (ofda.get("generic_name") or []):
            if n:
                k = molecule_canonical(n)
                if k:
                    fda_generic.setdefault(k, set()).add(sid)
        for n in (ofda.get("substance_name") or []):
            if n:
                k = molecule_canonical(n)
                if k:
                    fda_substance.setdefault(k, set()).add(sid)
    result["fda_scanned"] = scanned
    result["fda_in_scope_rx"] = in_scope
    note(f"  fda scanned={scanned:,} in_scope_rx={in_scope:,}")

    note("running the funnel over the distinct CMS pairs")
    stages = {"1_brand_exact": 0, "2_brand_normalized": 0,
              "3_ingredient": 0, "4_residual": 0}
    vol = {k: 0.0 for k in stages}
    residual = []
    for p in pairs:
        b = (p["_id"].get("b") or "").strip()
        g = (p["_id"].get("g") or "").strip()
        c = p["clms"]
        if b.upper() in fda_brand:
            stages["1_brand_exact"] += 1; vol["1_brand_exact"] += c
        elif brand_join_key(b) and ("NORM::" + brand_join_key(b)) in fda_brand:
            stages["2_brand_normalized"] += 1; vol["2_brand_normalized"] += c
        elif (molecule_canonical(b) in fda_substance or molecule_canonical(b) in fda_generic
              or molecule_canonical(g) in fda_substance or molecule_canonical(g) in fda_generic):
            stages["3_ingredient"] += 1; vol["3_ingredient"] += c
        else:
            stages["4_residual"] += 1; vol["4_residual"] += c
            if len(residual) < 200:
                residual.append({"brand": b, "generic": g, "clms": c, "cost": p["cost"]})
    result["funnel_pairs"] = stages
    result["funnel_claims"] = vol
    total_pairs = sum(stages.values()) or 1
    total_vol = sum(vol.values()) or 1.0
    result["funnel_pct_pairs"] = {k: round(100 * v / total_pairs, 2) for k, v in stages.items()}
    result["funnel_pct_claims"] = {k: round(100 * v / total_vol, 2) for k, v in vol.items()}
    result["residual_examples"] = sorted(residual, key=lambda r: -r["clms"])[:60]

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, default=str)
    note("DONE -> " + OUT)
    for k in sorted(stages):
        note(f"  {k:<20} pairs={stages[k]:>6} ({result['funnel_pct_pairs'][k]:>5.2f}%)  "
             f"claims={vol[k]:>15,.0f} ({result['funnel_pct_claims'][k]:>5.2f}%)")


main()
