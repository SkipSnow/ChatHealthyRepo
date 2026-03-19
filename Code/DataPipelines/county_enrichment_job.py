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
