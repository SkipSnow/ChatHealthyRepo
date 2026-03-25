---
adr_id: ADR-0006
title: Two MongoDB Clusters — FrontEnd and Pipeline
status: DECIDED
risk: Low
framework: framework_02
created_by: Skip + Claude
created_at: 2026-03-25
---

## Decision
Two MongoDB Atlas clusters: ChatHealthyFrontEndCluster (user-facing) and ChatHealthyPipelineCluster (ingestion).

## Rationale
Pipeline cluster gets hammered by ingestion jobs, enrichment runs, and embeddings. Frontend cluster must be fast and stable for live user queries. Keeping them separate protects UX from pipeline load spikes.

## Constraints
- App 2 (Chat) reads only from ChatHealthyFrontEndCluster
- App 3 (Pipelines) writes to ChatHealthyPipelineCluster
- copy_to_frontend.py handles cross-cluster data promotion

## Components
FindCareChat backend, DataPipelines
