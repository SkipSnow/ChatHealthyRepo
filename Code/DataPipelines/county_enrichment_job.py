# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""CountyEnrichmentJob — stub.

Accepts a worker result and returns success for all records.
County enrichment logic will be designed and implemented in a future sprint.
"""

import logging


class CountyEnrichmentJob:

    def enrich(self, config: dict) -> dict:
        worker_id = config.get("worker_id", "?")
        num_records = config.get("worker_result", {}).get("num_records", 0)
        logging.info(
            "CountyEnrichmentJob: worker %s, %d records (stub — returning success)",
            worker_id,
            num_records,
        )
        return {"worker_id": worker_id, "success": True, "failed": []}
