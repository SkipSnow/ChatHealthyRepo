---
adr_id: ADR-0009
title: PERSIST_MODE — debug vs hipaa
status: DECIDED
risk: High
framework: framework_02
created_by: Skip + Claude
created_at: 2026-03-25
---

## Decision
PERSIST_MODE environment variable controls what gets persisted: debug (full prompt + response) in dev/QA, hipaa (HIPAA two-tier consent flow) in prod.

## Rationale
Dev and QA need full conversation logs for debugging. Prod is a healthcare system — patient data requires explicit consent before storage. Two-tier consent: verbatim transcript or de-identified summary.

## Constraints
- PERSIST_MODE=debug must NEVER be set in prod
- hipaa mode enforces consent gate before any chat history is stored
- Safety incidents (IP + timestamp) are always persisted regardless of PERSIST_MODE
- Legal review of safety incident fields pending

## Components
FindCareChat backend
