---
adr_id: ADR-0008
title: main Instead of master — Modern GitHub Standard
status: DECIDED
risk: Low
framework: framework_02
created_by: Claude
created_at: 2026-03-25
---

## Decision
Production branch is named main, not master.

## Rationale
GitHub default standard since 2020. master is legacy nomenclature. Renamed on 2026-03-25 before framework_02 was finalized. No operational impact. GPT's framework_02 spec referenced master based on prior convention — this is the authoritative decision.

## Constraints
- All GitHub Actions workflows reference main
- Cloudflare Pages prod project tracks main branch of ChatHealthyWebSite
- GPT to acknowledge this deviation from original framework_02 spec

## Components
All — affects all workflows and deployment targets
