# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from chathealthy_frontend_lib import ChatHealthyLoggingService


class SplashEndpoint:
    """GET /splash — placeholder splash served when EvaluateCare takes
    ownership of the page. Wrapper consumes `{html: "..."}`."""

    def __init__(self):
        self.log = ChatHealthyLoggingService()
    def __call__(self):
        self.log.info("CONTROL TRANSFER: EvaluateCare has taken ownership of the page")
        return {
            "html": (
                '<div style="text-align:center;padding: 1em;">'
                '<div style="font-size: 1em;font-weight:700;color:#1f2937;">EvaluateCare</div>'
                '<div style="font-size: 1em;font-weight:600;color:#6b7280;margin-top: 1em;">is still unimplemented.</div>'
                '</div>'
            )
        }
