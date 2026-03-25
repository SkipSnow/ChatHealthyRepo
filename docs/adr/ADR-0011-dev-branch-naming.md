---
adr_id: ADR-0011
title: dev Retained as Integration Branch Name
status: DECIDED
risk: Low
framework: framework_02
created_by: Claude
created_at: 2026-03-25
---

## Decision
Integration branch is named dev, not develop.

## Rationale
dev branch is already wired to Cloudflare Pages chathealthy-dev project (chathealthy-dev.pages.dev). Renaming to develop requires Cloudflare Pages reconfiguration with no technical benefit. Deviation from framework_02 naming convention is intentional and documented.

## Constraints
- Cloudflare Pages chathealthy-dev project tracks dev branch of ChatHealthyWebSite
- All feature branches merge to dev via PR
- Only CI/CD merges dev to main
- GPT to acknowledge this deviation

## Components
All — affects all workflows and deployment targets
