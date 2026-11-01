"""Execution step 10 -- port Provider_v_5 to relational for the DE+AL providers.

WHY. T2's `state` currently comes from the Part D claim's Prscrbr_State_Abrvtn,
which is CMS's record of the prescriber. The firm's own provider record carries
practice addresses with their own state, county FIPS, RUCC and urban standing.
Those are two different assertions and the chain must use the firm's, with the
disagreement measured rather than assumed away. T2 is also still missing
nucc_codes, which must come from taxonomies[].code and NOT from Prscrbr_Type.

SCOPE. Only the NPIs that appear in partd (18,759). Provider_v_5 holds
9,671,888 documents; the experiment needs the ones it actually prescribes with.

LEFT OUT, by operator instruction: the embedding on the NUCC/specialty
collection. The taxonomy CODES and LABELS come across; the vectors do not.

Five tables, flat, each carrying load_seq:
  provider                   one row per NPI
  provider_taxonomy          one row per NPI x taxonomy  (the NUCC specialty)
  provider_practice_address  one row per NPI x practice address
  provider_business_address  one row per NPI
  provider_license           one row per NPI x license
  nucc_specialty             the NUCC vocabulary, embeddings excluded
"""
import csv
import io
import json
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()
from dotenv import load_dotenv  # noqa: E402

load_dotenv(r"C:\CHATHealthyLLC\.env")
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\exec_10_provider_result_v2.txt"
CHUNK = 2000


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def pg():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def s(v):
    return "" if v is None else str(v)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("EXECUTION STEP 10 -- Provider_v_5 -> relational, DE+AL providers")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    con = pg(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='3600s'")

    cur.execute("SELECT DISTINCT prscrbr_npi FROM partd WHERE prscrbr_npi <> ''")
    filled_npis = sorted({r[0] for r in cur.fetchall()})
    emit("NPIs with a prescription FILLED in DE/AL: %s" % f"{len(filled_npis):,}")

    DDL = {
        "provider": """
            load_seq bigserial PRIMARY KEY, npi text, entity_type_code text,
            entity_type_code_label text, provider_last_name_legal_name text,
            provider_first_name text, provider_middle_name text,
            provider_credential_text text, provider_sex_code text,
            is_sole_proprietor text, primary_taxonomy_code text,
            can_prescribe boolean, is_homeopathic boolean,
            is_primary_care boolean, urban boolean, is_active boolean,
            provider_enumeration_date text, last_update_date text,
            filled_in_test_state boolean, office_in_test_state boolean""",
        "provider_taxonomy": """
            load_seq bigserial PRIMARY KEY, npi text, seq int, code text,
            code_label text, reference_status text, is_primary boolean""",
        "provider_practice_address": """
            load_seq bigserial PRIMARY KEY, npi text, seq int, line1 text,
            line2 text, city text, state text, zip text, county_fips text,
            county_name text, county_source text, rucc int, urban boolean,
            latitude double precision, longitude double precision,
            geo_source text, geo_precision text""",
        "provider_business_address": """
            load_seq bigserial PRIMARY KEY, npi text, line1 text, line2 text,
            city text, state text, zip text, county_fips text""",
        "provider_license": """
            load_seq bigserial PRIMARY KEY, npi text, seq int, raw jsonb""",
    }
    for t, cols in DDL.items():
        cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute(f"CREATE TABLE {t} ({cols})")
    emit("created %d provider tables" % len(DDL))

    mc = ChatHealthyMongoUtilities().getConnection(
        "claudeCodeAgent", "ChatHealthyDataPipelines")
    src = mc["PipelinePublicHealthData"]["Provider_v_5"]

    # The second population: providers whose BILLING OFFICE is in DE or AL.
    # This is a different fact from where a prescription was filled, and it
    # cannot be derived from the claim file -- it needs the firm's own record.
    emit("scanning Provider_v_5 for billing offices in DE/AL ...")
    office_npis = set()
    for d in src.find({"$or": [{"practice_addresses.state": {"$in": ["DE", "AL"]}},
                               {"business_address.state": {"$in": ["DE", "AL"]}}]},
                      {"_id": 0, "npi": 1}):
        v = d.get("npi")
        if v:
            office_npis.add(str(v))
    emit("NPIs with a BILLING OFFICE in DE/AL: %s" % f"{len(office_npis):,}")
    npis = sorted(set(filled_npis) | office_npis)
    emit("union to port: %s  (filled-only=%s office-only=%s both=%s)"
         % (f"{len(npis):,}",
            f"{len(set(filled_npis)-office_npis):,}",
            f"{len(office_npis-set(filled_npis)):,}",
            f"{len(office_npis & set(filled_npis)):,}"))

    filled_set = set(filled_npis)
    buf = {k: io.StringIO() for k in DDL}
    wr = {k: csv.writer(v) for k, v in buf.items()}
    counts = {k: 0 for k in DDL}
    found = 0
    t0 = time.time()

    PROJ = {"_id": 0, "npi": 1, "entity_type_code": 1,
            "entity_type_code_label": 1, "provider_last_name_legal_name": 1,
            "provider_first_name": 1, "provider_middle_name": 1,
            "provider_credential_text": 1, "provider_sex_code": 1,
            "is_sole_proprietor": 1, "primary_taxonomy_code": 1,
            "can_prescribe": 1, "is_homeopathic": 1, "is_primary_care": 1,
            "urban": 1, "active": 1, "provider_enumeration_date": 1,
            "last_update_date": 1, "taxonomies": 1, "practice_addresses": 1,
            "business_address": 1, "licenses": 1}

    def flush():
        for t in DDL:
            b = buf[t]
            b.seek(0)
            cols = [c.split()[0] for c in DDL[t].replace("\n", " ").split(",")
                    if c.strip() and "bigserial" not in c]
            cur.copy_expert("COPY %s (%s) FROM STDIN WITH (FORMAT csv)"
                            % (t, ",".join(cols)), b)
            buf[t] = io.StringIO()
            wr[t] = csv.writer(buf[t])

    for i in range(0, len(npis), CHUNK):
        batch = npis[i:i + CHUNK]
        for d in src.find({"npi": {"$in": batch}}, PROJ):
            found += 1
            npi = s(d.get("npi"))
            act = (d.get("active") or {})
            wr["provider"].writerow([
                npi, s(d.get("entity_type_code")),
                s(d.get("entity_type_code_label")),
                s(d.get("provider_last_name_legal_name")),
                s(d.get("provider_first_name")), s(d.get("provider_middle_name")),
                s(d.get("provider_credential_text")), s(d.get("provider_sex_code")),
                s(d.get("is_sole_proprietor")), s(d.get("primary_taxonomy_code")),
                "true" if d.get("can_prescribe") else "false",
                "true" if d.get("is_homeopathic") else "false",
                "true" if d.get("is_primary_care") else "false",
                "true" if d.get("urban") else "false",
                "true" if act.get("is_active") else "false",
                s(d.get("provider_enumeration_date")), s(d.get("last_update_date")),
                "true" if npi in filled_set else "false",
                "true" if npi in office_npis else "false"])
            counts["provider"] += 1

            prim = s(d.get("primary_taxonomy_code"))
            for n, tx in enumerate(d.get("taxonomies") or []):
                code = s(tx.get("code"))
                wr["provider_taxonomy"].writerow([
                    npi, n, code, s(tx.get("code_label")),
                    s(tx.get("reference_status")),
                    "true" if (code and code == prim) else "false"])
                counts["provider_taxonomy"] += 1

            for n, a in enumerate(d.get("practice_addresses") or []):
                cty = (a.get("county") or {})
                co = (a.get("coordinates") or {})
                wr["provider_practice_address"].writerow([
                    npi, n, s(a.get("line1")), s(a.get("line2")),
                    s(a.get("city")), s(a.get("state")), s(a.get("zip")),
                    s(cty.get("fips")), s(cty.get("name")), s(cty.get("source")),
                    cty.get("rucc") if isinstance(cty.get("rucc"), int) else "",
                    "true" if cty.get("urban") else "false",
                    co.get("latitude") if isinstance(co.get("latitude"), (int, float)) else "",
                    co.get("longitude") if isinstance(co.get("longitude"), (int, float)) else "",
                    s(co.get("source")), s(co.get("precision"))])
                counts["provider_practice_address"] += 1

            ba = d.get("business_address") or {}
            if ba:
                wr["provider_business_address"].writerow([
                    npi, s(ba.get("line1")), s(ba.get("line2")),
                    s(ba.get("city")), s(ba.get("state")), s(ba.get("zip")),
                    s((ba.get("county") or {}).get("fips"))])
                counts["provider_business_address"] += 1

            for n, lic in enumerate(d.get("licenses") or []):
                wr["provider_license"].writerow(
                    [npi, n, json.dumps(lic, default=str)])
                counts["provider_license"] += 1

        flush()
        emit("  fetched %s/%s NPIs, matched %s  %.0fs"
             % (f"{min(i+CHUNK,len(npis)):,}", f"{len(npis):,}", f"{found:,}",
                time.time() - t0))

    for ddl in (
        "CREATE INDEX pv_npi ON provider (npi)",
        "CREATE INDEX pvt_npi ON provider_taxonomy (npi)",
        "CREATE INDEX pvt_code ON provider_taxonomy (code)",
        "CREATE INDEX pvpa_npi ON provider_practice_address (npi)",
        "CREATE INDEX pvpa_state ON provider_practice_address (state)",
        "CREATE INDEX pvba_npi ON provider_business_address (npi)",
        "CREATE INDEX pvl_npi ON provider_license (npi)",
    ):
        cur.execute(ddl)
    for t in DDL:
        cur.execute(f"ANALYZE {t}")
    emit("indexed")

    # ---- the NUCC vocabulary, embeddings EXCLUDED -------------------------
    meta = mc["PipelinePublicHealthData"]["SpecialtyMetaData_v_5"]
    one = meta.find_one({}) or {}
    drop = [k for k in one if "embed" in k.lower() or "vector" in k.lower()]
    emit("SpecialtyMetaData_v_5 fields excluded as embeddings: %s"
         % (", ".join(drop) if drop else "(none found)"))
    cur.execute("DROP TABLE IF EXISTS nucc_specialty")
    cur.execute("""CREATE TABLE nucc_specialty (
                     load_seq bigserial PRIMARY KEY, nucc_code text, raw jsonb)""")
    b = io.StringIO(); w = csv.writer(b)
    n = 0
    for d in meta.find({}, {"_id": 0}):
        for k in drop:
            d.pop(k, None)
        code = (d.get("nucc_code") or d.get("code") or d.get("_id") or "")
        w.writerow([str(code), json.dumps(d, default=str)])
        n += 1
    b.seek(0)
    cur.copy_expert("COPY nucc_specialty (nucc_code,raw) FROM STDIN WITH (FORMAT csv)", b)
    cur.execute("CREATE INDEX ns_code ON nucc_specialty (nucc_code)")
    cur.execute("ANALYZE nucc_specialty")
    emit("nucc_specialty=%s rows (embeddings excluded)" % f"{n:,}")

    emit()
    emit("=== PORT RESULT ===")
    for t in list(DDL) + ["nucc_specialty"]:
        cur.execute(f"SELECT count(*) FROM {t}")
        emit("  %-28s %10s" % (t, f"{cur.fetchone()[0]:,}"))
    emit()
    emit("  NPIs requested=%s matched in Provider_v_5=%s missing=%s"
         % (f"{len(npis):,}", f"{counts['provider']:,}",
            f"{len(npis)-counts['provider']:,}"))

    emit()
    emit("=== THE STATE SWAP -- CMS claim state vs the firm's practice state ===")
    cur.execute("""
        WITH claim AS (SELECT DISTINCT prscrbr_npi npi, prscrbr_state_abrvtn st
                       FROM partd),
             firm AS (SELECT npi, array_agg(DISTINCT state) sts
                      FROM provider_practice_address
                      WHERE state <> '' GROUP BY npi)
        SELECT c.st AS claim_state,
               count(*) AS providers,
               count(*) FILTER (WHERE f.sts IS NULL) AS no_firm_address,
               count(*) FILTER (WHERE f.sts IS NOT NULL AND c.st = ANY(f.sts)) AS agrees,
               count(*) FILTER (WHERE f.sts IS NOT NULL AND NOT (c.st = ANY(f.sts))) AS disagrees
        FROM claim c LEFT JOIN firm f ON f.npi = c.npi
        GROUP BY 1 ORDER BY 1""")
    emit("  %-6s %11s %16s %10s %11s"
         % ("claim", "providers", "no firm addr", "agrees", "DISAGREES"))
    for st, p, no, ag, dis in cur.fetchall():
        emit("  %-6s %11s %16s %10s %11s"
             % (st, f"{p:,}", f"{no:,}", f"{ag:,}", f"{dis:,}"))
    emit()
    emit("  examples where they disagree:")
    cur.execute("""
        WITH claim AS (SELECT DISTINCT prscrbr_npi npi, prscrbr_state_abrvtn st
                       FROM partd),
             firm AS (SELECT npi, array_agg(DISTINCT state) sts
                      FROM provider_practice_address WHERE state<>'' GROUP BY npi)
        SELECT c.npi, c.st, f.sts FROM claim c JOIN firm f ON f.npi=c.npi
        WHERE NOT (c.st = ANY(f.sts)) LIMIT 8""")
    for npi, st, sts in cur.fetchall():
        emit("    %s  claim=%s  firm practice states=%s" % (npi, st, sts))

    emit()
    emit("=== NUCC SPECIALTY NOW AVAILABLE (was absent from T2) ===")
    cur.execute("""SELECT count(DISTINCT npi), count(*), count(DISTINCT code)
                   FROM provider_taxonomy""")
    a, b_, c_ = cur.fetchone()
    emit("  providers with a taxonomy=%s rows=%s distinct NUCC codes=%s"
         % (f"{a:,}", f"{b_:,}", f"{c_:,}"))
    cur.execute("""SELECT t.code, max(t.code_label), count(DISTINCT t.npi)
                   FROM provider_taxonomy t GROUP BY 1 ORDER BY 3 DESC LIMIT 10""")
    for code, label, n_ in cur.fetchall():
        emit("    %-12s %-44s %s providers" % (code, str(label)[:44], f"{n_:,}"))

    emit()
    emit("=== CLAIM-SPECIALTY TEXT vs NUCC -- why Prscrbr_Type is not the specialty ===")
    cur.execute("""
        SELECT count(DISTINCT p.prscrbr_npi) FROM partd p
        WHERE EXISTS (SELECT 1 FROM provider_taxonomy t WHERE t.npi=p.prscrbr_npi)""")
    emit("  providers with both a claim specialty text and a NUCC code: %s"
         % f"{cur.fetchone()[0]:,}")
    cur.execute("""
        SELECT p.prscrbr_type, max(t.code_label), count(DISTINCT p.prscrbr_npi)
        FROM partd p JOIN provider_taxonomy t ON t.npi=p.prscrbr_npi AND t.is_primary
        GROUP BY 1 ORDER BY 3 DESC LIMIT 8""")
    emit("  %-28s %-40s %s" % ("Prscrbr_Type (claim text)", "primary NUCC label", "providers"))
    for ty, lab, n_ in cur.fetchall():
        emit("  %-28s %-40s %s" % (str(ty)[:28], str(lab)[:40], f"{n_:,}"))
    con.close()
    emit()
    emit("written to " + OUT)


main()
