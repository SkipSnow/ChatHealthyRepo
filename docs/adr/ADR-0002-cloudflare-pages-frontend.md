---
adr_id: ADR-0002
title: Cloudflare Pages for All Frontend Hosting
status: DECIDED
risk: Low
framework: framework_02
created_by: Skip
created_at: 2026-03-25
---

## Decision
All frontend (static website + React app) is hosted on Cloudflare Pages. Azure Static Web Apps will not be used.

## Rationale
Cloudflare Pages is free. Azure Static Web Apps is paid with no material benefit at current scale. Azure is reserved for compute only (App Service, Functions).

## Constraints
- React frontend deploys to Cloudflare Pages via GitHub Actions (Wrangler)
- VITE_API_URL injected at build time per environment
- No Azure SWA will be created for any frontend component

## Components
Website, FindCareChat frontend
