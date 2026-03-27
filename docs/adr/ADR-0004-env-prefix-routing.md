---
adr_id: ADR-0004
title: ENV_PREFIX Pattern for Environment-Aware Data Routing
status: DECIDED
risk: Low
framework: framework_02
created_by: Claude
created_at: 2026-03-25
---

## Decision
A single ENV_PREFIX environment variable (dev/qa/prod) controls all database and blob storage routing.

## Rationale
One variable, everything follows. No code changes between environments — only configuration. Eliminates environment-specific code paths.

## Constraints
- ENV_PREFIX set in Azure App Service config per deployment slot
- Database names: {ENV_PREFIX}_FindCare, {ENV_PREFIX}_PublicHealthData, {ENV_PREFIX}_MachineBrain
- Blob containers: {ENV_PREFIX}-{containerName}
- Default value: dev (safe fallback)

## Components
FindCareChat backend, DataPipelines
