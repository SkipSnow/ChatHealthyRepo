# ChatHealthy — FindCare

AI-driven healthcare provider search and network analysis. **Pre-Alpha.**
Built and designed by [Skip Snow](https://chathealthy.ai) — available for consulting engagements.

## What it does

FindCare helps consumers find appropriate healthcare providers and gives
organizations tools to evaluate, analyze, and grow provider networks.

## Architecture

Three cleanly separated applications:

| Layer | Host | Stack |
|---|---|---|
| Static Website | Cloudflare | HTML/CSS/JS |
| Conversational UX | HuggingFace | React, FastAPI, Anthropic Claude |
| Data Pipelines | Azure Functions | Python, Azure Durable Functions, MongoDB |

**Data pipeline highlights:**
- Ingests the full CMS NPPES public provider dataset (~8M records, ~8GB CSV)
- 50-worker fan-out via Azure Durable Functions — target load time under 1 hour
- Streaming blob reads, byte-aligned partitioning, idempotent upserts
- County enrichment, reconciliation, and Pushover alerting on completion

Full architecture and design documents: [chathealthy.ai](https://chathealthy.ai)

## License

Available for evaluation and inspection.
Use governed by the FindCare Evaluation License (FEL-1.0) — see [Legal/Licence.txt](Legal/Licence.txt).
© 2026 Skip Snow
