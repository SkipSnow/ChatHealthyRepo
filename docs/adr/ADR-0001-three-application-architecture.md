---
adr_id: ADR-0001
title: Three-Application Architecture
status: DECIDED
risk: Low
framework: framework_02
created_by: Skip + GPT + Claude
created_at: 2026-03-25
---

## Decision
The system is exactly three applications: Static Website, Conversational UX (Chat), and Data Pipelines.

## Rationale
Strict separation of concerns allows each application to be deployed, scaled, and maintained independently. Prevents coupling between user-facing UX and heavy data ingestion workloads.

## Constraints
- App 1 embeds App 2 via iframe only — no business logic
- App 2 reads DB freely, never writes to PublicHealthData
- App 3 owns all PublicHealthData writes exclusively
- App 2 → App 3 via HTTP POST /api/Router with Bearer token only

## Components
Website, FindCareChat, DataPipelines
