#!/usr/bin/env python3
"""Cleanup pipeline data for fresh test slate."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "FrontEndApplicationLib" / "src"))

from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

log = ChatHealthyLoggingService()

# Load .env
env_file = Path(__file__).parent.parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()

try:
    log.info("Connecting to MongoDB...")
    client = ChatHealthyMongoUtilities().getConnection("pipelineEditor")
    log.info("Connected to MongoDB")

    collections_to_clear = [
        ("chathealthypipelines", "pipeline.discrepancies"),
        ("chathealthypipelines", "pipeline.discrepancy_reports"),
        ("chathealthypipelines", "pipeline.run_counters"),
        ("admin", "PipelineDiscrepancyReports"),
    ]

    for db_name, coll_name in collections_to_clear:
        try:
            coll = client[db_name][coll_name]
            count = coll.count_documents({})
            if count > 0:
                result = coll.delete_many({})
                log.info("Cleared %s.%s: deleted %d documents", db_name, coll_name, result.deleted_count)
            else:
                log.info("%s.%s already empty", db_name, coll_name)
        except Exception as e:
            log.error("Failed to clear %s.%s: %s", db_name, coll_name, e)

    # Clear audit log
    log_file = Path(__file__).parent.parent.parent / "architecture" / "EngineeringRuleEnforcement" / "ArchitectureDesignAndAuditDocs" / "llm_classify_shell_invocation.log"
    if log_file.exists():
        log_file.unlink()
        log.info("Cleared audit log")
    else:
        log.info("Audit log already cleared")

    log.info("Cleanup complete — pipeline data reset to clean slate")
    client.close()
except Exception as e:
    log.error("Cleanup failed: %s", e)
    exit(1)
