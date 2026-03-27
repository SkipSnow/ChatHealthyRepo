---
adr_id: ADR-0010
title: Safety Incidents — IP + Timestamp Only Pending Legal Review
status: DECIDED
risk: High
framework: framework_02
created_by: Skip + Claude
created_at: 2026-03-25
---

## Decision
Safety incidents are logged with IP address and timestamp only. No message content, no user identity.

## Rationale
When a safety emergency is detected, the session is locked for 1 hour. The incident must be recorded for audit purposes regardless of consent. Minimum data (IP + timestamp) limits PHI exposure while maintaining an audit trail. Full logging requirements pending legal review.

## Constraints
- Safety incidents always persisted — no consent gate, no PERSIST_MODE check
- Fields: ip, triggered_at, expires_at, trigger_message (redacted/omitted pending legal)
- Legal review required before adding any additional fields in prod
- Stored in {ENV_PREFIX}_FindCare.safety_incidents

## Components
FindCareChat backend
