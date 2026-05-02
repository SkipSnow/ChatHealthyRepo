# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import logging


class SplashEndpoint:
    """GET /splash — placeholder splash served when SharedServices takes
    ownership of the page. Wrapper consumes `{html: "..."}`."""

    def __init__(self):
        self.log = logging.getLogger("shared_services.splash")

    def __call__(self):
        self.log.info("CONTROL TRANSFER: SharedServices has taken ownership of the page")
        return {
            "html": (
                '<div style="text-align:center;padding:20px;">'
                '<div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div>'
                '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
                '</div>'
            )
        }
