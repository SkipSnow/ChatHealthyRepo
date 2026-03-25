---
adr_id: ADR-0003
title: FastAPI on Azure App Service B1 — Port from Gradio/HuggingFace
status: DECIDED
risk: Moderate
framework: framework_02
created_by: Skip + Claude
created_at: 2026-03-25
---

## Decision
Port the HuggingFace Gradio app to FastAPI on Azure App Service B1. HuggingFace stays live until the port is tested and proven.

## Rationale
Gradio proved too brittle for production use. FastAPI gives full control over the API contract, streaming, safety gating, and observability. Azure App Service B1 is the cheapest compute tier that supports always-on, custom domains, and deployment slots.

## Constraints
- HuggingFace space stays live until React+FastAPI is smoke-tested
- Cutover via chat-url.txt update only — no code change required
- All app.py logic must be ported: safety gate, tools, Me class, HIPAA consent flow

## Components
FindCareChat backend
