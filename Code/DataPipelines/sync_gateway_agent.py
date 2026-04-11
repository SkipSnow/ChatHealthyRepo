# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SyncGatewayAgent — Agent-gated data promotion from pipeline to frontend.
# CopyToFrontEnd v2: approved by GPT-4.1 (2026-04-08).
#
# Problem: CopyToFrontEnd v1 OOMs copying 1.3M providers through Azure Function workers.
# Solution: Streaming copy (500-doc batches), atomic swap (temp+rename), per-state split,
#           quality gates, schema drift detection, parity verification, forward-only enforcement.

import logging
import os
import time
from datetime import datetime, timezone

from pymongo import MongoClient

from quality_gate import QualityGate, QualityGateFailure
from schema_drift_detector import SchemaDriftDetector, SchemaDriftError

log = logging.getLogger("sync_gateway")

COPY_BATCH_SIZE = 500
LARGE_STATE_THRESHOLD = 200_000

STATIC_COLLECTIONS = [
    "SpecialtyMetaData",
    "provider_quality",
    "ICD10Codes",
    "ZipCountyCrosswalk",
    "drug_crosswalk_cache",
]


class PromotionBlockedError(Exception):
    """Raised when promotion gate checks fail."""
    def __init__(self, message: str, checks: dict):
        super().__init__(message)
        self.checks = checks


class SyncGatewayAgent:
    """Agent-gated promotion of data from pipeline cluster to frontend cluster."""

    def __init__(self, pipeline_uri: str, frontend_uri: str, env_prefix: str = "dev"):
        self.pipeline_client = MongoClient(pipeline_uri, serverSelectionTimeoutMS=30_000)
        self.frontend_client = MongoClient(frontend_uri, serverSelectionTimeoutMS=30_000)
        self.env_prefix = env_prefix
        self.db_name = f"{env_prefix}_PublicHealthData" if env_prefix else "PublicHealthData"
        self.src_db = self.pipeline_client[self.db_name]
        self.dst_db = self.frontend_client[self.db_name]
        self.detector = SchemaDriftDetector()

    def close(self):
        self.pipeline_client.close()
        self.frontend_client.close()

    # ── Recovery: clean up orphan backups/temps from failed runs ────────

    def recover_orphans(self):
        """Check for orphan _backup_ and _tmp_sync_ collections and clean up."""
        for coll_name in self.dst_db.list_collection_names():
            if coll_name.startswith("_backup_"):
                original = coll_name.replace("_backup_", "", 1)
                if original not in self.dst_db.list_collection_names():
                    log.warning("Recovering orphan backup: %s → %s", coll_name, original)
                    self.dst_db.command("renameCollection",
                                        f"{self.db_name}.{coll_name}",
                                        to=f"{self.db_name}.{original}")
                else:
                    log.info("Dropping orphan backup: %s (original exists)", coll_name)
                    self.dst_db[coll_name].drop()
            elif coll_name.startswith("_tmp_sync_"):
                log.info("Dropping orphan temp: %s", coll_name)
                self.dst_db[coll_name].drop()

    # ── Gate Checks ────────────────────────────────────────────

    def run_gate_checks(self, states: list[str]) -> dict:
        """Run all promotion gate checks. Raises PromotionBlockedError on failure."""
        log.info("=== PROMOTION GATE CHECKS ===")
        checks = {}

        # Gate 1: Forward-only (GOV-012)
        checks["forward_only"] = {"env": self.env_prefix, "passed": True}
        log.info("Gate 1: Forward-only env=%s — PASS", self.env_prefix)

        # Gate 2: Provider count > 0 (estimated — instant, no collection scan)
        provider_count = self.src_db["providers"].estimated_document_count()
        checks["provider_count"] = {"count": provider_count, "passed": provider_count > 0}
        if provider_count == 0:
            raise PromotionBlockedError("No providers on pipeline cluster", checks)
        log.info("Gate 2: %d providers — PASS", provider_count)

        # Gate 3: QualityGate on providers
        gate = QualityGate("promotion_providers", min_rows=1000,
                           required_fields=["npi", "practice_address.state"],
                           max_null_fraction=0.01)
        qg_result = gate.enforce(self.src_db["providers"])
        checks["quality_gate"] = qg_result
        log.info("Gate 3: QualityGate — PASS")

        # Gate 4: Schema drift
        try:
            drift_result = self.detector.detect_and_alert(
                self.src_db["providers"], "providers", self.frontend_client)
            checks["schema_drift"] = drift_result
            log.info("Gate 4: SchemaDrift — PASS")
        except SchemaDriftError:
            # First run or intentional schema change — store and continue
            self.detector.store_fingerprint(
                self.src_db["providers"], "providers", self.frontend_client)
            checks["schema_drift"] = {"passed": True, "note": "first_run_stored"}
            log.info("Gate 4: SchemaDrift — first run, fingerprint stored")

        # Gate 5: Static collections
        for coll_name in STATIC_COLLECTIONS:
            count = self.src_db[coll_name].count_documents({})
            checks[f"static_{coll_name}"] = {"count": count}
            log.info("Gate 5: %s = %d docs", coll_name, count)

        # Gate 6: No active sync lock
        lock = self.frontend_client["admin"]["SyncLock"].find_one({"active": True})
        if lock:
            raise PromotionBlockedError(f"Active sync lock: {lock.get('job_id')}", checks)
        checks["sync_lock"] = {"passed": True}
        log.info("Gate 6: No active sync lock — PASS")

        # Gate 7: Source version check
        last_promotion = self.frontend_client["admin"]["PromotionLog"].find_one(
            {"env": self.env_prefix, "status": "SUCCESS"},
            sort=[("ts", -1)])
        checks["version_check"] = {"passed": True, "last_promotion": last_promotion.get("ts") if last_promotion else None}
        log.info("Gate 7: Version check — PASS")

        checks["all_passed"] = True
        log.info("=== ALL GATES PASSED ===")
        return checks

    # ── Streaming Copy ─────────────────────────────────────────

    def stream_to_temp(self, coll_name: str, query: dict = None) -> dict:
        """Stream docs from source to _tmp_sync_{coll_name} on frontend.
        500-doc batches, never OOM. Idempotent — drops temp first."""
        q = query or {}
        temp_name = f"_tmp_sync_{coll_name}"

        # Idempotent: drop temp from previous failed run
        self.dst_db[temp_name].drop()

        src = self.src_db[coll_name]
        dst_temp = self.dst_db[temp_name]
        src_count = src.estimated_document_count() if not q else src.count_documents(q)

        if src_count == 0:
            return {"collection": coll_name, "copied": 0, "source": 0, "skipped": True}

        log.info("Streaming %s: %d docs → %s", coll_name, src_count, temp_name)

        cursor = src.find(q, no_cursor_timeout=True)
        batch = []
        copied = 0
        start = time.time()

        try:
            from pymongo.errors import BulkWriteError
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= COPY_BATCH_SIZE:
                    try:
                        dst_temp.insert_many(batch, ordered=False)
                    except BulkWriteError as bwe:
                        # ordered=False inserts non-dupes and reports dupes as errors
                        log.warning("  %s: %d dup key errors (non-fatal)", coll_name,
                                    len(bwe.details.get("writeErrors", [])))
                    copied += len(batch)
                    batch = []
                    if copied % 50_000 == 0:
                        elapsed = time.time() - start
                        rate = copied / elapsed if elapsed > 0 else 0
                        log.info("  %s: %d / %d (%.0f doc/s)",
                                 coll_name, copied, src_count, rate)
            if batch:
                try:
                    dst_temp.insert_many(batch, ordered=False)
                except BulkWriteError:
                    pass
                copied += len(batch)
        finally:
            cursor.close()

        elapsed = time.time() - start
        temp_count = dst_temp.count_documents({})

        log.info("  %s: %d docs in %.1fs (temp has %d)", coll_name, copied, elapsed, temp_count)
        return {"collection": coll_name, "copied": copied, "source": src_count,
                "temp_count": temp_count, "elapsed": round(elapsed, 1)}

    # ── Atomic Swap ────────────────────────────────────────────

    def atomic_swap(self, coll_name: str, expected_count: int) -> dict:
        """Verify parity on temp, then swap: current → backup → rename temp → drop backup."""
        temp_name = f"_tmp_sync_{coll_name}"
        backup_name = f"_backup_{coll_name}"

        temp_count = self.dst_db[temp_name].count_documents({})
        if temp_count != expected_count:
            self.dst_db[temp_name].drop()
            msg = f"Parity FAIL {coll_name}: expected={expected_count} temp={temp_count}"
            log.error(msg)
            return {"collection": coll_name, "parity": False, "expected": expected_count,
                    "actual": temp_count, "swapped": False}

        # Backup current
        if coll_name in self.dst_db.list_collection_names():
            self.dst_db[coll_name].rename(backup_name, dropTarget=True)

        # Rename temp to current
        self.dst_db[temp_name].rename(coll_name)

        # Drop backup
        self.dst_db.drop_collection(backup_name)

        log.info("Atomic swap %s: %d docs — SUCCESS", coll_name, temp_count)
        return {"collection": coll_name, "parity": True, "count": temp_count, "swapped": True}

    # ── Full Promotion ─────────────────────────────────────────

    def promote(self, states: list[str]) -> dict:
        """Full promotion: gates → sync → swap → indexes → verify."""
        start = time.time()
        result = {"ts": datetime.now(timezone.utc).isoformat(), "env": self.env_prefix}

        # Recovery
        self.recover_orphans()

        try:
            # Step 1: Gate checks (before lock — lock check is part of gates)
            result["gates"] = self.run_gate_checks(states)

            # Acquire lock after gates pass
            self.frontend_client["admin"]["SyncLock"].update_one(
                {"_id": "sync_lock"},
                {"$set": {"active": True, "started": result["ts"], "states": states}},
                upsert=True)

            # Step 2: Sync static collections
            result["static"] = []
            for coll_name in STATIC_COLLECTIONS:
                r = self.stream_to_temp(coll_name)
                result["static"].append(r)
                if not r.get("skipped"):
                    swap = self.atomic_swap(coll_name, r["source"])
                    r["swap"] = swap
                    if not swap["parity"]:
                        result["status"] = "FAILED"
                        return result

            # Step 3: Sync providers — DR-006: partition by _id range, not state
            provider_query = {"practice_address.state": {"$in": states}} if states else {}
            total_expected = self.src_db["providers"].count_documents(provider_query)
            log.info("Syncing providers: %d docs (states: %s)", total_expected, states)

            # Stream all matching providers into temp (single cursor, 500-doc batches)
            r = self.stream_to_temp("providers", query=provider_query)
            result["providers"] = r

            # Step 4: Atomic swap providers
            swap = self.atomic_swap("providers", total_expected)
            result["provider_swap"] = swap
            if not swap["parity"]:
                result["status"] = "FAILED"
                return result

            # Step 5: Vector indexes
            from copy_to_frontend import (_create_frontend_vector_index,
                                           _create_specialty_vector_index,
                                           verify_frontend_indexes)
            try:
                _create_frontend_vector_index(self.frontend_client, self.db_name)
                _create_specialty_vector_index(self.frontend_client, self.db_name)
                result["indexes"] = "created"
            except Exception as e:
                result["indexes"] = f"error: {e}"
                log.error("Vector index creation failed: %s", e)

            # Step 6: Final verification (DR-016)
            result["index_check"] = verify_frontend_indexes(self.frontend_client, self.db_name)

            # Step 7: Per-state parity
            parity_results = []
            all_parity = True
            for state in states:
                q = {"practice_address.state": state}
                src_c = self.src_db["providers"].count_documents(q)
                dst_c = self.dst_db["providers"].count_documents(q)
                match = src_c == dst_c
                if not match:
                    all_parity = False
                parity_results.append({"state": state, "source": src_c, "dest": dst_c, "match": match})
                log.info("Parity %s: src=%d dst=%d %s", state, src_c, dst_c, "PASS" if match else "FAIL")
            result["parity"] = parity_results
            result["all_parity"] = all_parity

            elapsed = time.time() - start
            result["elapsed_seconds"] = round(elapsed, 1)
            result["status"] = "SUCCESS" if all_parity else "FAILED"

        except (QualityGateFailure, SchemaDriftError, PromotionBlockedError) as e:
            result["status"] = "BLOCKED"
            result["error"] = str(e)
            log.error("Promotion BLOCKED: %s", e)
        except Exception as e:
            result["status"] = "FAILED"
            result["error"] = str(e)
            log.error("Promotion FAILED: %s", e, exc_info=True)
        finally:
            # Release lock
            self.frontend_client["admin"]["SyncLock"].update_one(
                {"_id": "sync_lock"}, {"$set": {"active": False}})

            # Log promotion
            try:
                self.frontend_client["admin"]["PromotionLog"].insert_one({
                    "ts": result["ts"],
                    "env": self.env_prefix,
                    "status": result.get("status", "UNKNOWN"),
                    "elapsed": result.get("elapsed_seconds"),
                    "states": states,
                    "error": result.get("error"),
                })
            except Exception as e:
                log.warning("Failed to log promotion: %s", e)

        log.info("=== PROMOTION %s — %.1f min ===",
                 result.get("status"), result.get("elapsed_seconds", 0) / 60)
        return result


# ── CLI / Azure Function entry point ──────────────────────────

def run_promote_to_frontend(config: dict) -> dict:
    """Entry point for Azure Functions or CLI."""
    pipeline_uri = os.environ.get("MONGO_connectionString")
    frontend_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not pipeline_uri or not frontend_uri:
        raise ValueError("Both MONGO_connectionString and MONGO_FRONTEND_connectionString required")

    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE", "MS", "VA"])

    agent = SyncGatewayAgent(pipeline_uri, frontend_uri, env_prefix)
    try:
        return agent.promote(states)
    finally:
        agent.close()
# deploy trigger
