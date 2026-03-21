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
| Conversational UX | HuggingFace | Gradio 5.22, OpenAI gpt-4o-mini, Claude Haiku |
| Data Pipelines | Azure Functions | Python, Azure Durable Functions, MongoDB |

**Data pipeline highlights:**
- Ingests the full CMS NPPES public provider dataset (~8M records, ~8GB CSV)
- 200-worker fan-out via Azure Durable Functions — first production run loaded 8.8M records in ~35 min
- Streaming blob reads, byte-aligned partitioning, idempotent upserts
- County enrichment via ZIP crosswalk (Pass 1) and Census Geocoder API (Pass 2)
- SparkPost email notification on completion

Full architecture and design documentation is published at **[chathealthy.ai](https://chathealthy.ai)** — including pipeline design, data models, and system architecture. The source for all documentation is in `Website/` in this repository.

## License

Available for evaluation and inspection.
Use governed by the FindCare Evaluation License (FEL-1.0) — see [Legal/Licence.txt](Legal/Licence.txt).
© 2026 Skip Snow
