# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from chathealthy_frontend_lib import ChatHealthyLoggingService


class SplashEndpoint:
    """POST /splash — placeholder splash served when EvaluateCare takes
    ownership of the page. Returns STRUCTURED DATA ONLY. The React
    EvaluateCareSplashWidget is the renderer."""

    def __init__(self):
        self.log = ChatHealthyLoggingService()

    def __call__(self):
        self.log.info("CONTROL TRANSFER: EvaluateCare has taken ownership of the page")
        return {
            "title": "EvaluateCare",
            "subtitle": "is still unimplemented.",
        }
