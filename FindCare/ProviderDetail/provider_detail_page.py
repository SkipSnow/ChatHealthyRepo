# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The provider-detail page handler, drained out of app.py.

app.py's /provider-detail route collects the request, proves the gateway
signature, and hands the turn here. A click-path detail (EPIC-006-F-002):
deterministic, no model runs.
"""
from __future__ import annotations

from chathealthy_lib.runtime_data_collections import (providers_coll,
                                                      specialty_meta_coll)

from ProviderDetail.provider_detail_models import (ProviderDetailInput,
                                                   ProviderDetailOutput)
from ProviderDetail.provider_detail_service import ProviderDetailService

_service = ProviderDetailService()


def detail(body: ProviderDetailInput) -> ProviderDetailOutput:
    """Look the provider up as the registry reports them at the moment the
    card was opened. The lookup reads our record and refreshes it against
    live NPPES."""
    return _service.lookup(
        entity_type=body.entity_type,
        provider_name=body.name or "",
        npi=body.npi,
        state=body.state or "",
        provider_coll=providers_coll(),
        specialty_meta_coll=specialty_meta_coll,
    )
