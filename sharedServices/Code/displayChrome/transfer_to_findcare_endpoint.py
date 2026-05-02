# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import logging


class TransferToFindCareEndpoint:
    """POST /transfer/to-findcare — SharedServices releases page ownership
    back to FindCare. Mirrors the EvaluateCare pattern."""

    def __init__(self):
        self.log = logging.getLogger("shared_services.transfer_to_findcare")

    def __call__(self):
        self.log.info("CONTROL TRANSFER: SharedServices → FindCare")
        return {"owner": "findcare", "reason": "filter_interaction"}
