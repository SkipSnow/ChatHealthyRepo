---
adr_id: ADR-0007
title: Machine Brain — MongoDB Persistent Architectural Memory
status: DECIDED
risk: Low
framework: framework_02
created_by: GPT + Skip
created_at: 2026-03-25
---

## Decision
Machine Brain is a MongoDB database (dev/qa/prod_MachineBrain on ChatHealthyFrontEndCluster) storing architectural decisions, patterns, and knowledge across sessions and agents.

## Rationale
AI agents (Claude, GPT) lose context between sessions. Machine Brain provides persistent memory so decisions are reused, conflicts are detected, and the system learns over time. It is also a sellable product component.

## Constraints
- Claude MUST query Machine Brain before any non-trivial implementation
- GPT writes to Machine Brain after architectural decisions
- All decisions must include risk level per Risk Model
- Required API: get_decisions(topic) and store_decision(decision)
- Enforcement rule: if Machine Brain is not queried, it does not exist

## Components
Code/Shared/machine_brain.py, all services
