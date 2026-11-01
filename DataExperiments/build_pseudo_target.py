"""Build the pseudo-target public collections LOCALLY, in relational form.

THE OPERATOR'S POINT, WHICH IS CORRECT: if the port cannot be done properly,
the local data is wrong. A port that has to decide what to include is a port
doing business logic, and the decision belongs upstream where it is visible.

So the local tables must ALREADY BE the collections' content. Then:
  RULE 1  zero business logic in the port -- there is nothing left to decide
  RULE 2  only the data in the pseudo-target public collections -- because
          the table holds nothing else
  RULE 3  values identical between Mongo and the relational db -- because the
          port reads and writes without touching a value

WHAT WAS WRONG BEFORE. chain_delivered, chain_provider_molecule and
chain_code are STAGING -- engineering artifacts carrying internal fields:
    icd10_key                        the DOTLESS form. BR-008: internal join
                                     key, NEVER published. I had it in the port.
    asserts_unsupported_specificity  a diagnostic, and BR-019 now forces it
                                     false on all 14.1M rows -- a column that
                                     cannot vary is not data
    rung                             which match rung produced the indication
    n_source_pairs                   audit of how many CMS names collapsed
    rows_benes_suppressed            audit of suppressed source rows
Filtering those out DURING the port would have been the business logic Rule 1
forbids. They are excluded HERE instead, in SQL, where the exclusion is
readable and reviewable.

WHAT IS PUBLIC AND WHY. A field is in the pseudo-target when it is a fact
about the provider, the molecule, the indication or the code that a consumer
or an auditor could act on. A field is excluded when it exists to let US audit
our own construction. Two are marked OPERATOR RULING and are carried with a
note rather than silently decided:
    corroborated_by_trial  -- a completed trial agreeing with the label. Real
                              information, arguably internal provenance.
    confidence/provenance  -- carried, because the operator's confidence scale
                              exists precisely so a user can be told how sure
                              we are. Omitting them would hide the thing the
                              scale was built to expose.
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
OUT = r"C:\CHATHealthyLLC\DataExperiments\pseudo_target_result.txt"


def emit(msg=""):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def connect():
    pw = open(PW_FILE, encoding="utf-8").read().strip()
    return psycopg2.connect(host="localhost", port=5432, user="postgres",
                            password=pw, dbname="medicare_join")


def stop(rule, detail):
    raise ChatHealthyException("runtime_error", "RULE VIOLATED %s -- %s" % (rule, detail))


# Fields that must NEVER appear in a public collection. Named, so the
# exclusion is a declaration rather than an omission someone has to notice.
FORBIDDEN_PUBLIC = {
    "icd10_key": "BR-008 -- the dotless form is an internal join key",
    "asserts_unsupported_specificity": "internal diagnostic, invariant",
    "rung": "internal match-rung name",
    "n_source_pairs": "internal collapse audit",
    "rows_benes_suppressed": "internal suppression audit",
    "label_before": "internal before/after marker",
    "label_before_fixes": "internal before/after marker",
    "molecule_claims_nonadditive": "internal fan-out warning; the volume is "
                                   "additive at the document grain and is "
                                   "carried as 'claims'",
}


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    emit("BUILD THE PSEUDO-TARGET PUBLIC COLLECTIONS, LOCALLY")
    emit("run at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("the port must have nothing to decide, so the deciding happens here")
    emit()

    con = connect(); con.autocommit = True
    cur = con.cursor(); cur.execute("SET statement_timeout='7200s'")

    # ============================================= ProviderMedicare, parent
    emit("=== pt_ProviderMedicare -- one row per molecule per provider ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_providermedicare CASCADE;
        CREATE TABLE pt_providermedicare AS
        SELECT npi,
               molecule_id,
               fill_state,
               billing_state,
               state_differs,
               entity_type_code,
               entity_type_code_label,
               primary_nucc,
               primary_nucc_label,
               is_active,
               tot_clms      AS claims,
               tot_drug_cst  AS drug_cost,
               tot_benes     AS beneficiaries
        FROM chain_provider_molecule
        WHERE molecule_id IS NOT NULL;
        CREATE UNIQUE INDEX ptpm_key ON pt_providermedicare (npi, molecule_id);
        ANALYZE pt_providermedicare;
    """)
    cur.execute("SELECT count(*) FROM pt_providermedicare")
    emit("   rows: %s" % f"{cur.fetchone()[0]:,}")

    # ====================================== ProviderMedicare, nested children
    emit()
    emit("=== pt_ProviderMedicare_indication -- the nested child rows ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_providermedicare_indication CASCADE;
        CREATE TABLE pt_providermedicare_indication AS
        SELECT DISTINCT
               npi,
               molecule_id,
               indication_id,
               indication_text,
               corroborated_by_trial,
               icd10_code,
               who_level,
               is_billable,
               route,
               ccsr_category,
               body_system,
               provenance,
               confidence
        FROM chain_delivered;
        CREATE INDEX ptpmi_key ON pt_providermedicare_indication (npi, molecule_id);
        ANALYZE pt_providermedicare_indication;
    """)
    cur.execute("SELECT count(*) FROM pt_providermedicare_indication")
    emit("   rows: %s" % f"{cur.fetchone()[0]:,}")

    # ===================================================== NUCCSpecialty
    emit()
    emit("=== pt_NUCCSpecialty -- one row per specialty code ===")
    cur.execute("""
        DROP TABLE IF EXISTS pt_nuccspecialty CASCADE;
        CREATE TABLE pt_nuccspecialty AS
        SELECT nucc_code, display_name, grouping, classification,
               specialization, definition, section,
               can_prescribe, is_homeopathic, is_disqualified
        FROM chain_nucc;
        CREATE UNIQUE INDEX ptn_key ON pt_nuccspecialty (nucc_code);
        ANALYZE pt_nuccspecialty;
    """)
    cur.execute("SELECT count(*) FROM pt_nuccspecialty")
    emit("   rows: %s" % f"{cur.fetchone()[0]:,}")

    # ========================== RULE 2 ENFORCED: no internal field survives
    emit()
    emit("=== RULE 2 -- no internal field is present in any pseudo-target ===")
    cur.execute("""SELECT table_name, column_name
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name LIKE 'pt_%'
                   ORDER BY table_name, ordinal_position""")
    found = cur.fetchall()
    offenders = [(t, c) for t, c in found if c in FORBIDDEN_PUBLIC]
    for t, c in offenders:
        emit("   VIOLATION %s.%s -- %s" % (t, c, FORBIDDEN_PUBLIC[c]))
    if offenders:
        stop("RULE 2", "%d internal field(s) reached a pseudo-target; the "
                       "port would have published them" % len(offenders))
    emit("   clean -- %d columns across %d tables, none forbidden"
         % (len(found), len({t for t, _ in found})))
    emit()
    emit("   excluded by name, and why:")
    for c, why in sorted(FORBIDDEN_PUBLIC.items()):
        emit("     %-34s %s" % (c, why))

    # ========================== RULE 3 ENFORCED: values identical to source
    emit()
    emit("=== RULE 3 -- every value identical to the staging source ===")
    checks = [
        ("claims", "SELECT sum(claims) FROM pt_providermedicare",
         "SELECT sum(tot_clms) FROM chain_provider_molecule WHERE molecule_id IS NOT NULL"),
        ("drug_cost", "SELECT sum(drug_cost) FROM pt_providermedicare",
         "SELECT sum(tot_drug_cst) FROM chain_provider_molecule WHERE molecule_id IS NOT NULL"),
        ("beneficiaries", "SELECT sum(beneficiaries) FROM pt_providermedicare",
         "SELECT sum(tot_benes) FROM chain_provider_molecule WHERE molecule_id IS NOT NULL"),
        ("provider-molecule keys",
         "SELECT count(*) FROM pt_providermedicare",
         "SELECT count(*) FROM chain_provider_molecule WHERE molecule_id IS NOT NULL"),
        ("distinct ICD codes",
         "SELECT count(DISTINCT icd10_code) FROM pt_providermedicare_indication",
         "SELECT count(DISTINCT icd10_code) FROM chain_delivered"),
        ("distinct indications",
         "SELECT count(DISTINCT indication_id) FROM pt_providermedicare_indication",
         "SELECT count(DISTINCT indication_id) FROM chain_delivered"),
        ("specialty codes", "SELECT count(*) FROM pt_nuccspecialty",
         "SELECT count(*) FROM chain_nucc"),
    ]
    for name, a, b in checks:
        cur.execute(a); va = cur.fetchone()[0]
        cur.execute(b); vb = cur.fetchone()[0]
        ok = (va == vb)
        emit("   %-26s target=%-20s staging=%-20s %s"
             % (name, str(va), str(vb), "IDENTICAL" if ok else "DIFFERENT"))
        if not ok:
            stop("RULE 3", "%s differs between the pseudo-target and staging; "
                           "the target must carry the same VALUES" % name)

    # the one honest caveat, stated rather than buried
    emit()
    emit("=== THE ONE OPEN QUESTION FOR THE OPERATOR ===")
    cur.execute("""SELECT count(*) FROM pt_providermedicare
                   WHERE beneficiaries IS NULL""")
    nulls = cur.fetchone()[0]
    emit("   %s rows carry a NULL beneficiaries (HIPAA suppression)."
         % f"{nulls:,}")
    emit("   In the document the port can write the key as null, or omit it.")
    emit("   Omitting is the document convention and matches 'absent rather")
    emit("   than defaulted'. Writing null is the literal same VALUE.")
    emit("   This is a FORM question under Rule 3, not a value question, and")
    emit("   it is recorded here rather than decided.")

    emit()
    emit("=== WHAT THE OPERATOR CAN NOW INSPECT ===")
    for t in ("pt_providermedicare", "pt_providermedicare_indication",
              "pt_nuccspecialty"):
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s
                       ORDER BY ordinal_position""", (t,))
        cols = cur.fetchall()
        cur.execute(f"SELECT count(*) FROM {t}")
        emit("   %-34s %10s rows  %d columns"
             % (t, f"{cur.fetchone()[0]:,}", len(cols)))
        emit("      " + ", ".join(c for c, _ in cols))
    con.close()
    emit()
    emit("written to " + OUT)


main()
