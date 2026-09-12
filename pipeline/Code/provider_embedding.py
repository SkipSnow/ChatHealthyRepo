# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""provider_embedding — re-export shim.

Single source of truth: chathealthy_lib.provider_embedding. Both the
pipeline (this module) and the FindCare backend re-embed path import from the
same canonical module so a provider record produces the same embedding text on
both sides."""

from chathealthy_lib.provider_embedding import (  # noqa: F401
    project,
    render,
    should_embed,
)
