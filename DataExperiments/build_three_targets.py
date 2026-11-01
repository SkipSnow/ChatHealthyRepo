"""Build the three pseudo-target collections, relationally, from the chain.

THE THREE TARGETS
  1 pt_nuccspecialty     the NUCC ROLLUP. The same shape as the provider
                         collection, aggregated at the NUCC specialty level,
                         from the DE and AL data.
  2 pt_fdadruglabel      the FDA file, 100% COMPLETE -- every label in the
                         corpus nationally, not a two-state subset. The two
                         states bound the CLAIMS; they do not bound the FDA
                         label set, and "100% complete" is explicit.
  3 pt_providermedicare  NPI x CMS drug -> molecule -> indication -> ICD-10,
                         one record per molecule per provider.

WHY THE ROLLUP IS HERE AND NOT IN THE PORT. Aggregation is business logic.
The port is nesting only. So the specialty rollup is computed in SQL, where
the aggregation is readable, and the port then carries it verbatim like
anything else.

WHAT THE ROLLUP SUMS, AND WHAT IT REFUSES TO SUM. Claims, cost and days are
summed across the providers of a specialty -- those are additive at that
grain. Beneficiary counts are NOT summed: CMS suppresses cells below 11
beneficiaries, so a sum over a suppressed column understates by an unknown
amount and would look like a measurement. The rollup carries the count of
providers instead, which is the thing it can actually know.
"""
import sys
import os
import time

import psycopg2

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
OUT = r"C:\CHATHealthyLLC\DataExperiments\build_three_targets_result.txt"

FORBIDDEN_PUBLIC = {
    "icd10_key": "BR-008 -- the dotless form is an internal join key",
    "asserts_unsupported_specificity": "internal diagnostic, invariant",
    "rung": "internal match-rung name",
    "n_source_pairs": "internal collapse audit",
    "rows_benes_suppressed": "internal suppression audit",
    "label_before": "internal before/after marker",
    "label_before_fixes": "internal before/after marker",
    "molecule_claims_nonadditive": "internal fan-out warning",
    "load_seq": "internal load counter",
    "source_blob": "internal provenance pointer",
    "match_set": "internal folded match key",
    "ident_set": "internal unfolded identity key",
}


def emit(m=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(m + "\n")
    _log.info("%s", m)


def stop(detail):
    raise ChatHealthyException("runtime_error", "BUILD FAILED -- " + detail)


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                           password=pw, dbname="medicare_join")
    con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='0'")

    emit("BUILD THE THREE PSEUDO-TARGET COLLECTIONS")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit()

    # ============================================ 3  ProviderMedicare parent
    emit("=== pt_providermedicare -- NPI x molecule ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_providermedicare CASCADE;
        CREATE TABLE pt_providermedicare AS
        SELECT npi, molecule_id, fill_state, billing_state, state_differs,
               entity_type_code, entity_type_code_label,
               primary_nucc, primary_nucc_label, is_active,
               tot_clms     AS claims,
               tot_drug_cst AS drug_cost,
               tot_benes    AS beneficiaries
        FROM chain_provider_molecule
        WHERE molecule_id IS NOT NULL;
        CREATE UNIQUE INDEX ptpm_key ON pt_providermedicare (npi, molecule_id);
        CREATE INDEX ptpm_nucc ON pt_providermedicare (primary_nucc);
        ANALYZE pt_providermedicare;
    """)
    cur.execute("SELECT count(*) FROM pt_providermedicare")
    n_prov = cur.fetchone()[0]
    emit("   rows: %s" % f"{n_prov:,}")
    if n_prov < 400000:
        stop("pt_providermedicare has only %d rows" % n_prov)

    emit()
    emit("=== pt_providermedicare_indication -- the nested children ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_providermedicare_indication CASCADE;
        CREATE TABLE pt_providermedicare_indication AS
        SELECT npi, molecule_id, indication_id, indication_text,
               corroborated_by_trial, icd10_code, who_level, is_billable,
               route, ccsr_category, body_system, provenance, confidence
        FROM chain_delivered;
        CREATE INDEX ptpmi_key ON pt_providermedicare_indication (npi, molecule_id);
        CREATE INDEX ptpmi_nucc_src ON pt_providermedicare_indication (icd10_code);
        ANALYZE pt_providermedicare_indication;
    """)
    cur.execute("SELECT count(*) FROM pt_providermedicare_indication")
    n_ind = cur.fetchone()[0]
    emit("   rows: %s" % f"{n_ind:,}")
    if n_ind < 1000000:
        stop("pt_providermedicare_indication has only %d rows" % n_ind)

    # ================================================ 1  the NUCC ROLLUP
    emit()
    emit("=== pt_nuccspecialty -- the NUCC ROLLUP, same shape, aggregated ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_nuccspecialty CASCADE;
        CREATE TABLE pt_nuccspecialty AS
        WITH prov AS (
          SELECT pm.primary_nucc        AS nucc_code,
                 pm.primary_nucc_label  AS nucc_label,
                 pm.fill_state,
                 count(DISTINCT pm.npi)         AS providers,
                 count(DISTINCT pm.molecule_id) AS molecules,
                 sum(pm.claims)                 AS claims,
                 sum(pm.drug_cost)              AS drug_cost
          FROM pt_providermedicare pm
          WHERE pm.primary_nucc IS NOT NULL
          GROUP BY 1,2,3)
        SELECT p.nucc_code,
               max(p.nucc_label)                AS nucc_label,
               n.display_name, n.grouping, n.classification,
               n.specialization, n.definition, n.section,
               n.can_prescribe, n.is_homeopathic, n.is_disqualified,
               sum(p.providers)                 AS providers,
               max(p.molecules)                 AS molecules,
               sum(p.claims)                    AS claims,
               sum(p.drug_cost)                 AS drug_cost
        FROM prov p
        LEFT JOIN chain_nucc n ON n.nucc_code = p.nucc_code
        GROUP BY p.nucc_code, n.display_name, n.grouping, n.classification,
                 n.specialization, n.definition, n.section,
                 n.can_prescribe, n.is_homeopathic, n.is_disqualified;
        CREATE UNIQUE INDEX ptn_key ON pt_nuccspecialty (nucc_code);
        ANALYZE pt_nuccspecialty;
    """)
    cur.execute("SELECT count(*) FROM pt_nuccspecialty")
    n_nucc = cur.fetchone()[0]
    emit("   specialties with DE/AL prescribing: %s" % f"{n_nucc:,}")
    if n_nucc < 100:
        stop("pt_nuccspecialty has only %d rows" % n_nucc)

    # the rollup's nested children: the same indication/code shape, at the
    # specialty grain. Aggregation in SQL, never in the port.
    emit()
    emit("=== pt_nuccspecialty_indication -- the rollup's nested children ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_nuccspecialty_indication CASCADE;
        CREATE TABLE pt_nuccspecialty_indication AS
        SELECT pm.primary_nucc                  AS nucc_code,
               i.indication_id,
               max(i.indication_text)           AS indication_text,
               i.icd10_code,
               max(i.who_level)                 AS who_level,
               bool_or(i.is_billable)           AS is_billable,
               max(i.route)                     AS route,
               max(i.ccsr_category)             AS ccsr_category,
               max(i.body_system)               AS body_system,
               -- weakest provenance on the path survives the rollup (BR-018)
               CASE WHEN bool_or(i.provenance='inference') THEN 'inference'
                    WHEN bool_or(i.provenance='adopted_vocabulary')
                      THEN 'adopted_vocabulary'
                    ELSE 'joined_coded_source' END AS provenance,
               min(i.confidence)                AS confidence,
               count(DISTINCT i.npi)            AS providers,
               count(DISTINCT i.molecule_id)    AS molecules
        FROM pt_providermedicare_indication i
        JOIN pt_providermedicare pm
          ON pm.npi = i.npi AND pm.molecule_id = i.molecule_id
        WHERE pm.primary_nucc IS NOT NULL
        GROUP BY 1,2,4;
        CREATE INDEX ptni_key ON pt_nuccspecialty_indication (nucc_code);
        ANALYZE pt_nuccspecialty_indication;
    """)
    cur.execute("SELECT count(*) FROM pt_nuccspecialty_indication")
    n_ni = cur.fetchone()[0]
    emit("   rows: %s" % f"{n_ni:,}")
    if n_ni < 10000:
        stop("pt_nuccspecialty_indication has only %d rows" % n_ni)

    # ================================================ 2  the FDA file, 100%
    emit()
    emit("=== pt_fdadruglabel -- the FDA file, 100% COMPLETE ===")
    emit("   every label in the corpus, nationally. The two states bound the")
    emit("   CLAIMS; they do not bound the FDA label set.")
    cur.execute("""
        DROP TABLE IF EXISTS pt_fdadruglabel CASCADE;
        CREATE TABLE pt_fdadruglabel AS
        SELECT l.set_id,
               max(l.spl_id)                           AS spl_id,
               max(l.effective_time)                   AS effective_time,
               max(l.version)                          AS version,
               max(nullif(btrim(l.product_type),''))   AS product_type,
               max(nullif(btrim(l.active_ingredient),'')) AS active_ingredient,
               max(nullif(btrim(l.description),''))    AS description,
               max(nullif(btrim(l.indications_and_usage),'')) AS indications_and_usage,
               max(nullif(btrim(l.how_supplied),''))   AS how_supplied,
               max(c.pediatric_use)                    AS pediatric_use,
               max(c.geriatric_use)                    AS geriatric_use,
               max(c.pregnancy)                        AS pregnancy,
               max(c.nursing_mothers)                  AS nursing_mothers,
               max(c.use_in_specific_populations)      AS use_in_specific_populations,
               max(c.labor_and_delivery)               AS labor_and_delivery,
               max(c.contraindications)                AS contraindications,
               max(c.boxed_warning)                    AS boxed_warning
        FROM fda_label l
        LEFT JOIN fda_label_cohort c ON c.set_id = l.set_id
        GROUP BY l.set_id;
        CREATE UNIQUE INDEX ptf_key ON pt_fdadruglabel (set_id);
        ANALYZE pt_fdadruglabel;
    """)
    cur.execute("SELECT count(*) FROM pt_fdadruglabel")
    n_fda = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT set_id) FROM fda_label")
    n_src = cur.fetchone()[0]
    emit("   labels: %s   source distinct set_id: %s" % (f"{n_fda:,}", f"{n_src:,}"))
    if n_fda != n_src:
        stop("the FDA file is not 100%% complete: %d of %d labels"
             % (n_fda, n_src))
    emit("   100%% COMPLETE -- every label in the corpus is present")

    # the FDA file's nested molecules: which molecules a label resolves to
    emit()
    emit("=== pt_fdadruglabel_molecule -- the label's molecules ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_fdadruglabel_molecule CASCADE;
        CREATE TABLE pt_fdadruglabel_molecule AS
        SELECT DISTINCT ml.set_id, ml.molecule_id, ml.rung AS match_rung
        FROM mol_label ml;
        CREATE INDEX ptfm_key ON pt_fdadruglabel_molecule (set_id);
        ANALYZE pt_fdadruglabel_molecule;
    """)
    cur.execute("SELECT count(*) FROM pt_fdadruglabel_molecule")
    emit("   label-to-molecule links: %s" % f"{cur.fetchone()[0]:,}")

    # ================================ RULE 2: no internal field survives
    emit()
    emit("=== RULE 2 -- no internal field in any pseudo-target ===")
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name LIKE 'pt_%'
                   ORDER BY table_name, ordinal_position""")
    found = cur.fetchall()
    bad = [(t, c) for t, c in found if c in FORBIDDEN_PUBLIC]
    for t, c in bad:
        emit("   VIOLATION %s.%s -- %s" % (t, c, FORBIDDEN_PUBLIC[c]))
    if bad:
        stop("%d internal field(s) reached a pseudo-target" % len(bad))
    emit("   clean -- %d columns across %d tables"
         % (len(found), len({t for t, _ in found})))

    emit()
    emit("=== WHAT WILL BE MIGRATED ===")
    for t in ("pt_providermedicare", "pt_providermedicare_indication",
              "pt_nuccspecialty", "pt_nuccspecialty_indication",
              "pt_fdadruglabel", "pt_fdadruglabel_molecule"):
        cur.execute(f"SELECT count(*) FROM {t}")
        emit("   %-38s %12s rows" % (t, f"{cur.fetchone()[0]:,}"))
    con.close()
    emit()
    emit("written to " + OUT)


main()
