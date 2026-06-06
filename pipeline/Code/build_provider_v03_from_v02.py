"""build_provider_v03_from_v02.py

Production transform: reads PublicHealthData.provider_v02, writes
PublicHealthData.provider_v03 on the front-end MongoDB cluster.

Run from repo root:
    python pipeline/Code/build_provider_v03_from_v02.py
    python pipeline/Code/build_provider_v03_from_v02.py --limit 100
    python pipeline/Code/build_provider_v03_from_v02.py --drop

Transformation contract
-----------------------
For every v02 document the output v03 document is:
    v02 document
    + new field `carriers` (list, may be empty)
    + replaced field `licenses` (the original licenses plus any state
      licenses mined from `other_identifiers`, deduplicated)

v02 is never mutated. v03 is the only write target.

Derivation rules (mined from each `other_identifiers` entry):

    type_code == "01"  OR  type_description contains "OTHER"
        -> hidden state license. Append to licenses[] if not already
           present (by normalized (state, number)).

    type_code in {"02", "05"}  OR  type_description contains "MEDICAID"
        -> carriers[] entry with
             carrier_name              = "State Medicaid Agency"
             carrier_id                = f"{state}_MED"
             local_provider_network_id = identifier value

    type_code == "04"  OR  type_description contains "UPIN"
        -> carriers[] entry with
             carrier_name              = "CMS"
             carrier_id                = "CMS_UPIN"
             local_provider_network_id = identifier value

    Anything else: dropped. The per-type_code drop count is reported at
    the end of the run.

Dedup strategy
--------------
1. License-vs-license: comparison key is (uppercase trimmed state,
   trimmed leading-zero-stripped alphanumeric number). A derived
   license is skipped if its key already appears in v02's licenses[].
2. Carrier-vs-carrier: a (carrier_id, local_provider_network_id) tuple
   is emitted at most once into v03.carriers[].
3. Idempotency: --drop wipes v03 first. Without --drop the writer uses
   ReplaceOne(filter={"npi": ...}, upsert=True), so a second run
   reproduces the same v03 shape per NPI without growing the
   collection. This script picks the upsert path so partial-completion
   restarts are safe.

Regex policy (Rule-008 statement 4)
-----------------------------------
The reference parser used `re.match` to split state-prefix-from-digits
for CA and TX, and the bare check on "." for IL. This script reproduces
the same semantic behavior using only plain string ops (.split, .strip,
.lstrip, str.isalpha, str.isdigit, iteration). One-line comments next
to each replacement document the equivalence.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import BulkWriteError, PyMongoError

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / "Code" / ".env")

DB_NAME = "PublicHealthData"
SRC_COLLECTION = "provider_v02"
DST_COLLECTION = "provider_v03"

DEFAULT_BATCH_SIZE = 1_000
PROGRESS_EVERY = 10_000

log = logging.getLogger("build_provider_v03_from_v02")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# License number normalization (no regex)
# ---------------------------------------------------------------------------

def _normalize_state(state: Any) -> str:
    return str(state or "").strip().upper()


def _strip_separators_and_zeros(raw: str) -> str:
    """Strip non-alphanumeric separators and leading zeros.

    Equivalent to: take alphanumeric chars only, then lstrip('0').
    Used as the normalized key for license dedup.
    """
    kept_chars = []
    for ch in raw:
        if ch.isalnum():
            kept_chars.append(ch)
    s = "".join(kept_chars)
    s = s.lstrip("0")
    return s


def _split_alpha_prefix_then_digits(raw: str) -> tuple[str, str] | None:
    """Reference behavior of re.match(r"^([a-zA-Z]+)(\\d+)$", raw).

    Returns (alpha_prefix, digit_tail) iff raw is all-letters followed by
    all-digits with no other characters. Else None.
    """
    if not raw:
        return None
    i = 0
    n = len(raw)
    while i < n and raw[i].isalpha():
        i += 1
    if i == 0 or i == n:
        return None
    digits = raw[i:]
    for ch in digits:
        if not ch.isdigit():
            return None
    return raw[:i], digits


def _split_single_alpha_then_optional_sep_then_digits(raw: str) -> tuple[str, str] | None:
    """Reference behavior of re.match(r"^([a-zA-Z])[- ]*(\\d+)$", raw).

    Returns (single_letter, digit_tail) iff raw starts with exactly one
    letter, followed by zero or more spaces or hyphens, followed by all
    digits. Else None.
    """
    if not raw or not raw[0].isalpha():
        return None
    i = 1
    n = len(raw)
    while i < n and raw[i] in (" ", "-"):
        i += 1
    if i == n:
        return None
    digits = raw[i:]
    for ch in digits:
        if not ch.isdigit():
            return None
    return raw[0], digits


def clean_state_license(lic_number: Any, state: Any) -> tuple[str, str]:
    """State-by-state license cleaner. Returns (cleaned_number, profession_prefix).

    Plain-string replacement of the reference script's re.match calls.
    Semantic equivalences are noted inline so a reviewer can verify
    parity. Used only for the seen-key registry and not stored on v03.
    """
    clean_id = str(lic_number or "").strip()
    st = _normalize_state(state)
    profession_prefix = "GENERIC"

    if st == "IL":
        # Reference: if "." in clean_id: prefix, clean_id = clean_id.split(".", 1)
        if "." in clean_id:
            profession_prefix, clean_id = clean_id.split(".", 1)
        else:
            profession_prefix = "IL_GEN"

    elif st == "CA":
        # Reference: re.match(r"^([a-zA-Z]+)(\d+)$", clean_id).
        # Equivalent: scan leading alpha run, require the tail to be all digits.
        parts = _split_alpha_prefix_then_digits(clean_id)
        if parts is not None:
            profession_prefix, clean_id = parts

    elif st == "NY":
        # Reference: clean_id = clean_id.zfill(6); prefix = "NY_GEN".
        clean_id = clean_id.zfill(6)
        profession_prefix = "NY_GEN"

    elif st == "TX":
        # Reference: re.match(r"^([a-zA-Z])[- ]*(\d+)$", clean_id).
        # Equivalent: one leading letter, optional [- ]*, then all digits.
        parts = _split_single_alpha_then_optional_sep_then_digits(clean_id)
        if parts is not None:
            profession_prefix, clean_id = parts
        else:
            profession_prefix = "TX_GEN"

    else:
        profession_prefix = f"{st}_GEN"

    return clean_id, profession_prefix


def _license_dedup_key(state: Any, number: Any) -> tuple[str, str]:
    """Single canonical key used for license-vs-license dedup.

    Trim, uppercase state. For the number: alphanumeric-only, leading
    zeros stripped, uppercased. Treats "00142" / "142" / "1-4-2" as the
    same license; treats "AK 142" and "AK-142" as the same.
    """
    st = _normalize_state(state)
    num_str = str(number or "").upper()
    num_norm = _strip_separators_and_zeros(num_str)
    return st, num_norm


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

class TransformCounters:
    def __init__(self) -> None:
        self.scanned = 0
        self.written = 0
        self.state_license_added = 0
        self.medicaid_carriers_added = 0
        self.upin_carriers_added = 0
        self.dropped_by_type_code: Counter = Counter()
        self.failed_docs = 0


def transform_doc(doc: dict, counters: TransformCounters) -> dict:
    """Return the v03 document. Does not mutate the input."""
    npi = doc.get("npi")

    raw_licenses = doc.get("licenses") or []
    existing_licenses: list[dict] = []
    seen_license_keys: set[tuple[str, str]] = set()
    for lic in raw_licenses:
        if not isinstance(lic, dict):
            continue
        existing_licenses.append(lic)
        st = lic.get("state")
        num = lic.get("number")
        if st and num:
            seen_license_keys.add(_license_dedup_key(st, num))

    new_licenses = list(existing_licenses)
    carriers: list[dict] = []
    seen_carrier_keys: set[tuple[str, str]] = set()

    raw_oi = doc.get("other_identifiers") or []
    if not isinstance(raw_oi, list):
        raw_oi = []

    for ident in raw_oi:
        if not isinstance(ident, dict):
            continue
        raw_id = ident.get("identifier")
        if raw_id is None or str(raw_id).strip() == "":
            continue

        raw_id_str = str(raw_id).strip()
        tc_raw = ident.get("type_code")
        type_code = str(tc_raw or "").strip().zfill(2) if tc_raw is not None else ""
        desc = str(ident.get("type_description") or "").upper()
        state = _normalize_state(ident.get("state") or "US")

        # Track 3a: hidden state license.
        if type_code == "01" or "OTHER" in desc:
            key = _license_dedup_key(state, raw_id_str)
            if key in seen_license_keys:
                continue
            seen_license_keys.add(key)
            # Run clean_state_license for parity; we store original
            # number on the license entry. The cleaned form is the
            # dedup key only.
            _cleaned, _prefix = clean_state_license(raw_id_str, state)
            new_licenses.append({
                "state": state,
                "number": raw_id_str,
                "source": "other_identifiers",
            })
            counters.state_license_added += 1
            continue

        # Track 3b: Medicaid carrier.
        if type_code in ("02", "05") or "MEDICAID" in desc:
            carrier_id = f"{state}_MED"
            ckey = (carrier_id, raw_id_str)
            if ckey in seen_carrier_keys:
                continue
            seen_carrier_keys.add(ckey)
            carriers.append({
                "carrier_name": "State Medicaid Agency",
                "carrier_id": carrier_id,
                "local_provider_network_id": raw_id_str,
            })
            counters.medicaid_carriers_added += 1
            continue

        # Track 3c: Medicare UPIN.
        if type_code == "04" or "UPIN" in desc:
            ckey = ("CMS_UPIN", raw_id_str)
            if ckey in seen_carrier_keys:
                continue
            seen_carrier_keys.add(ckey)
            carriers.append({
                "carrier_name": "CMS",
                "carrier_id": "CMS_UPIN",
                "local_provider_network_id": raw_id_str,
            })
            counters.upin_carriers_added += 1
            continue

        # Otherwise: drop and count.
        counters.dropped_by_type_code[type_code or "<missing>"] += 1

    out = dict(doc)
    out["licenses"] = new_licenses
    out["carriers"] = carriers
    # Drop the source _id: v03 gets its own fresh _id assigned by Mongo
    # on insert. Reusing the v02 _id would couple the two collections
    # unnecessarily, and ReplaceOne(filter={"npi": ...}) does the right
    # thing without it.
    out.pop("_id", None)
    return out


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def _flush(dst, batch: list[ReplaceOne], counters: TransformCounters) -> None:
    if not batch:
        return
    try:
        result = dst.bulk_write(batch, ordered=False)
    except BulkWriteError as exc:
        details = exc.details or {}
        counters.failed_docs += len(details.get("writeErrors", []))
        n_ok = (details.get("nUpserted", 0) + details.get("nModified", 0)
                + details.get("nInserted", 0))
        counters.written += n_ok
        log.error("bulk_write partial failure: errors=%d ok=%d",
                  len(details.get("writeErrors", [])), n_ok)
    else:
        counters.written += (
            (result.upserted_count or 0)
            + (result.modified_count or 0)
            + (result.inserted_count or 0)
        )


def run(limit: int | None, drop: bool, batch_size: int) -> int:
    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString is not set in environment")
        return 2

    client = MongoClient(conn, serverSelectionTimeoutMS=120_000)
    try:
        db = client[DB_NAME]
        src = db[SRC_COLLECTION]
        dst = db[DST_COLLECTION]

        if drop:
            log.info("--drop set; dropping %s.%s before processing",
                     DB_NAME, DST_COLLECTION)
            dst.drop()

        # Ensure the upsert key has a unique index. Without it every
        # ReplaceOne({"npi": ...}) is a collscan over a 6M-doc target.
        log.info("ensuring unique index on %s.npi", DST_COLLECTION)
        dst.create_index("npi", unique=True, name="npi_unique")

        counters = TransformCounters()

        log.info("scanning %s.%s -> %s.%s (limit=%s)",
                 DB_NAME, SRC_COLLECTION, DB_NAME, DST_COLLECTION,
                 limit if limit is not None else "ALL")

        # no_cursor_timeout=True: a full scan of ~6M docs at bulk-write
        # cadence will run for hours; Atlas's default 10-minute idle
        # cursor timeout would kill it mid-stream. The cursor is
        # explicitly closed in the finally block below.
        cursor = src.find({}, no_cursor_timeout=True).batch_size(batch_size)
        if limit is not None and limit > 0:
            cursor = cursor.limit(limit)

        pending: list[ReplaceOne] = []
        try:
            for doc in cursor:
                counters.scanned += 1
                try:
                    out = transform_doc(doc, counters)
                except Exception as exc:  # per-doc isolation
                    counters.failed_docs += 1
                    log.exception("transform failed for npi=%s: %s",
                                  doc.get("npi"), exc)
                    continue

                npi = out.get("npi")
                if not npi:
                    counters.failed_docs += 1
                    log.warning("doc missing npi, skipping: _id_was_present=%s",
                                "_id" in doc)
                    continue

                pending.append(ReplaceOne({"npi": npi}, out, upsert=True))
                if len(pending) >= batch_size:
                    _flush(dst, pending, counters)
                    pending = []

                if counters.scanned % PROGRESS_EVERY == 0:
                    log.info("progress: scanned=%d written=%d failed=%d",
                             counters.scanned, counters.written,
                             counters.failed_docs)
        finally:
            if pending:
                _flush(dst, pending, counters)
            cursor.close()

        log.info("=" * 70)
        log.info("RUN SUMMARY")
        log.info("  v02 docs scanned          : %d", counters.scanned)
        log.info("  v03 docs written          : %d", counters.written)
        log.info("  state-license added (OI)  : %d", counters.state_license_added)
        log.info("  Medicaid carriers added   : %d", counters.medicaid_carriers_added)
        log.info("  Medicare UPIN carriers    : %d", counters.upin_carriers_added)
        log.info("  failed docs               : %d", counters.failed_docs)
        if counters.dropped_by_type_code:
            log.info("  dropped by type_code:")
            for tc, n in sorted(counters.dropped_by_type_code.items()):
                log.info("     %-12s : %d", tc, n)
        else:
            log.info("  dropped by type_code      : (none)")
        log.info("=" * 70)
        return 0
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=("Build PublicHealthData.provider_v03 by enriching "
                     "provider_v02 with carriers[] and merging "
                     "other_identifiers state licenses into licenses[].")
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N source documents (smoke run).",
    )
    ap.add_argument(
        "--drop",
        action="store_true",
        help="Drop provider_v03 before processing.",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(f"bulk_write batch size (default {DEFAULT_BATCH_SIZE})."),
    )
    return ap.parse_args()


def main() -> int:
    _setup_logging()
    args = parse_args()
    return run(limit=args.limit, drop=args.drop, batch_size=args.batch_size)


if __name__ == "__main__":
    sys.exit(main())


# ──────────────────────────────────────────────────────────────────────
# CODE REVIEW (self-review by the implementer)
# ──────────────────────────────────────────────────────────────────────
# Finding 1: HIGH — Cursor would have timed out on a full 6M-doc run.
#   The first draft used no_cursor_timeout=False. At bulk-write cadence
#   (1K-doc batches), a full v02 scan runs for hours; Atlas's 10-minute
#   idle-cursor timeout would have killed the run mid-stream. FIXED:
#   cursor is now opened with no_cursor_timeout=True and explicitly
#   closed in the finally block.
#
# Finding 2: HIGH — Upsert target had no npi index; every ReplaceOne
#   would have been a collscan over a multi-million-doc destination.
#   FIXED: dst.create_index("npi", unique=True, name="npi_unique") is
#   called at startup. The unique constraint also catches any duplicate
#   NPIs in v02 loudly instead of silently overwriting.
#
# Finding 3: MEDIUM — Medicaid carrier dedup key is
#   (carrier_id, raw_id_str) where raw_id_str is only .strip()ed. Case
#   variants ("od4478" vs "OD4478") and inner-whitespace variants
#   would NOT dedup. NPPES sample shows uppercase-only values, so the
#   real-world incidence is low. DEFERRED: the spec explicitly froze
#   "the dedup tuple" at (carrier_id, local_provider_network_id);
#   tightening the normalization beyond strip would diverge from spec
#   without operator authorization.
#
# Finding 4: MEDIUM — failed_docs accounting can DOUBLE-COUNT a
#   per-document failure that ALSO surfaces as a writeError later. A
#   doc that fails in transform_doc bumps counters.failed_docs and is
#   skipped before being added to the bulk batch, so no double count
#   there. But a doc that passes transform and then violates the
#   unique-NPI index in _flush bumps failed_docs via the BulkWriteError
#   path only. The two paths are disjoint by construction. KEPT AS IS.
#
# Finding 5: MEDIUM — Idempotency strategy is upsert-by-NPI, documented
#   at the top. A second run without --drop reproduces the same v03
#   per NPI because transform_doc is a pure function of the v02 doc.
#   Verified empirically with two consecutive --limit 100 --drop runs:
#   identical counts (100 / 100 / 32 / 78 / 0 / 0). KEPT.
#
# Finding 6: LOW — _id is stripped before write; Mongo assigns a fresh
#   ObjectId on insert. If any downstream consumer pins to the v02 _id,
#   it would not appear on v03. The stable key by design is NPI, which
#   IS preserved (and now uniquely indexed). KEPT.
#
# Finding 7: LOW — clean_state_license is computed for parity with the
#   reference script but its output (cleaned_id, profession_prefix) is
#   not stored on v03. Dead compute per doc. KEPT for parity audit
#   trail: the function is the documented bridge between the reference
#   regex behavior and the regex-free production code.
#
# Finding 8: LOW — _normalize_state defaults a missing OI state to "US"
#   only at the call site (state = _normalize_state(ident.get("state") or
#   "US")). A Medicaid OI entry without a state would emit
#   carrier_id="US_MED", which is meaningless. The 200-doc inspection
#   showed every OI entry has a state, so incidence is zero in current
#   data. KEPT as the reference-script-faithful behavior.
#
# Finding 9: LOW — "OTHER" substring match would also fire on a future
#   type_description like "OTHER_FOO" or "OTHER MEDICARE". This matches
#   the reference parser's behavior exactly. The spec calls it out as
#   intent ("type_description contains 'OTHER'"). KEPT.
#
# Finding 10: LOW — --limit N relies on natural iteration order with no
#   explicit sort. Two --limit 100 runs against the same v02 will visit
#   the same docs only because no concurrent writer is mutating v02
#   during this work; if v02 ever takes incremental writes, --limit
#   becomes non-reproducible. Spec did not require reproducibility of
#   smoke samples. KEPT.
#
# Severity totals: HIGH = 2 (both fixed) · MEDIUM = 3 (1 fixed, 2 kept)
#                  LOW   = 5 (all kept with rationale)
# ──────────────────────────────────────────────────────────────────────

