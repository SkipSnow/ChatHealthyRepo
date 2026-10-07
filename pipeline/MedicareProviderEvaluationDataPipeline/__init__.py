# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Medicare Provider Evaluation Data Pipeline (EPIC-010-F-007).

A pipeline instance on the generic run lifecycle (EPIC-010-F-001) that builds
three public collections — ProviderMedicare (per NPI), SpecialtyMedicare (per
NUCC), and the Normalized Indication Map (per indication). It harvests Part D
prescribed drugs and Part B billed procedures; both feed the provider-evaluation
axis EvaluateCare reads. See LLD Part III.
"""
