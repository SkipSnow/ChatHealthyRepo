# ChatHealthy — DevOps Design
**Date:** 2026-03-25
**Authors:** Skip Snow (product), Claude (solution architecture)
**For:** GPT enterprise architecture review

---

## 1. Team Structure

| Role | Agent | Responsibilities |
|---|---|---|
| Product Director | Skip Snow | Direction, final approval, ships to prod |
| Enterprise Architect | GPT | System design, cross-cutting concerns, standards, scalability, security posture, QA |
| Solution Architect + Builder | Claude | Solution architecture, implementation, unit tests, deployment |

---

## 2. Three-Application Architecture

| # | Application | Host | Code Path |
|---|---|---|---|
| 1 | Static Website | Cloudflare Pages | `Website/` |
| 2 | Chat Frontend + FastAPI Backend | Cloudflare Pages + Azure App Service | `Code/ConversationalUX/FindCareChat/` |
| 3 | Data Pipelines | Azure Functions | `Code/DataPipelines/` |

### Boundary Rules
- App 1 embeds App 2 via iframe only — no business logic
- App 2 handles all UX, LLM calls, DB reads, writes to application DBs
- App 3 owns all writes to `PublicHealthData` — ingestion, embeddings, multi-agent workflows
- App 2 → App 3: HTTP POST to `/api/Router` with Bearer token

---

## 3. Environments

| Environment | Branch | Frontend | Backend | DB Prefix |
|---|---|---|---|---|
| Dev | `dev` | `chathealthy-dev.pages.dev` | Azure App Service dev slot | `dev_` |
| QA | `qa` (future) | `qa.chathealthy.ai` | Azure App Service QA slot | `qa_` |
| Prod | `master` | `chathealthy.ai` | Azure App Service prod slot | `prod_` |

---

## 4. MongoDB — Database Naming Convention

Two clusters, concern-separated:

| Cluster | Purpose |
|---|---|
| `ChatHealthyFrontEndCluster` | All frontend/UX data — fast, stable, user-facing |
| `ChatHealthyPipelineCluster` | All pipeline data — tolerates heavy ingestion load |

### Database Names

| Environment | Frontend DB | Pipeline DB |
|---|---|---|
| Dev | `dev_FindCare` | `dev_PublicHealthData` |
| QA | `qa_FindCare` | `qa_PublicHealthData` |
| Prod | `prod_FindCare` | `prod_PublicHealthData` |

- Collections retain their exact names across all environments
- Schema validation: none in dev, warn mode in QA, strict in prod (future feature)

---

## 5. Environment Variable Pattern

Single `ENV_PREFIX` controls all data routing:

```
ENV_PREFIX=dev    # → dev_FindCare, dev_PublicHealthData, dev-[blob-container]
ENV_PREFIX=qa     # → qa_FindCare, qa_PublicHealthData, qa-[blob-container]
ENV_PREFIX=prod   # → prod_FindCare, prod_PublicHealthData, prod-[blob-container]
```

FastAPI reads:
```python
ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
DB_NAME = f"{ENV_PREFIX}_FindCare"
BLOB_CONTAINER = f"{ENV_PREFIX}-{BASE_CONTAINER_NAME}"
```

---

## 6. Iframe URL Injection

The static website iframes the chat frontend. URL is injected at Cloudflare Pages build time:

- `Website/chat-url.txt` — committed per branch, contains the iframe src URL
- Build command: `CHAT_URL=$(cat chat-url.txt) && sed -i "s|%%CHAT_URL%%|$CHAT_URL|g" index.html`
- Changing the iframe URL = one-line commit to `chat-url.txt`, no dashboard changes

---

## 7. Persistence Policy

| Environment | Normal Chat | Safety Incident |
|---|---|---|
| Dev | Full prompt + response (debug) | IP + timestamp |
| QA | Full prompt + response (debug) | IP + timestamp |
| Prod | HIPAA two-tier consent flow | IP + timestamp (legal review pending) |

Controlled by:
```
PERSIST_MODE=debug   # dev + QA
PERSIST_MODE=hipaa   # prod
```

---

## 8. Blob Storage Convention

Same prefix pattern as databases:

| Environment | Container |
|---|---|
| Dev | `dev-[containerName]` |
| QA | `qa-[containerName]` |
| Prod | `prod-[containerName]` |

---

## 9. CI/CD

| Trigger | Action |
|---|---|
| Push to `dev` → `ChatHealthyWebSite` dev branch | Cloudflare auto-deploys dev site |
| Push to `master` → `ChatHealthyWebSite` master branch | Cloudflare auto-deploys prod site |
| Push to `master` → `Code/ConversationalUX/` | GitHub Actions → HuggingFace (temporary, until React port) |
| Push to `master` → `Code/DataPipelines/` | GitHub Actions → Azure Functions |

Website deploy: `git subtree push --prefix=Website cloudflare [branch]`
Engineer-controlled — no manual Cloudflare dashboard steps.

---

## 10. Bot Collaboration Protocol (Proposed)

### Team directories in repo:
```
claude_thoughts/    ← Claude drops questions, decisions, proposals for GPT
gpt_thoughts/       ← GPT drops architecture feedback, concerns, approvals
```

### Flow:
- Claude writes to `claude_thoughts/` when work needs GPT review
- GPT writes to `gpt_thoughts/` in response
- Skip relays between sessions (near term)
- Cron-driven async handoff (stretch goal — Claude polls `gpt_thoughts/` on a schedule)

### File naming convention:
```
YYYY-MM-DD_[topic].md
```

---

## Questions for GPT

1. Is `ENV_PREFIX` the right pattern, or should DB and blob use separate env vars?
2. Schema validation approach — is warn→strict the right MongoDB progression for QA→prod?
3. Bot collaboration protocol — is the `claude_thoughts/` + `gpt_thoughts/` directory pattern sound, or is there a better async handoff design?
4. Any cross-cutting concerns with the three-application boundary rules as defined?
5. Persistence policy — is IP + timestamp sufficient for safety incident audit logging at this stage, or are there legal/compliance considerations we should design for now?
