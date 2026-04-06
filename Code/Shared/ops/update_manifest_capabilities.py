# Copyright (c) 2026 Skip Snow. All rights reserved.
import json, os

CAPABILITIES = {
    "Code/ConversationalUX/FindCareChat/backend/application/facades/evaluate_care_facade.py": ["Facade for EvaluateCareQuality (UAT 3, 8)", "Clinical trials search orchestration", "Provider detail lookup via NPI"],
    "Code/ConversationalUX/FindCareChat/backend/application/facades/find_care_facade.py": ["Facade for FindCare (UAT 1, 2)", "Provider search with vector+taxonomy fallback", "Specialty identification with NUCC codes"],
    "Code/ConversationalUX/FindCareChat/backend/application/tool_models/clinical_trials_models.py": ["Pydantic validation for clinical trials inputs"],
    "Code/ConversationalUX/FindCareChat/backend/application/tool_models/consent_models.py": ["Pydantic validation for lead capture", "Pydantic validation for unanswerable questions"],
    "Code/ConversationalUX/FindCareChat/backend/application/tool_models/provider_search_models.py": ["Pydantic validation for provider search", "Pydantic validation for specialty queries"],
    "Code/ConversationalUX/FindCareChat/backend/application/tool_router.py": ["Allowlist tool dispatch (GOV-004)", "Pydantic input validation (F-05)", "Anthropic tool_use block handling", "Chat history injection"],
    "Code/ConversationalUX/FindCareChat/backend/domain/evaluate_care_quality/clinical_trials_service.py": ["ClinicalTrials.gov API search (RECRUITING filter)", "Google Routes travel distance+duration", "Trial location extraction and formatting"],
    "Code/ConversationalUX/FindCareChat/backend/domain/evaluate_care_quality/provider_detail_service.py": ["NPI Registry CMS API lookup", "State medical board links (DE, MS, VA)", "Research site URL construction"],
    "Code/ConversationalUX/FindCareChat/backend/domain/find_care/provider_search_service.py": ["MongoDB Atlas vector search (text-embedding-3-large)", "Taxonomy fallback with NUCC codes", "FIPS-to-county mapping (ASN-4AFBDA)", "Multi-stage state/city/county filtering"],
    "Code/ConversationalUX/FindCareChat/backend/domain/find_care/specialty_service.py": ["Dual-pipeline specialty ID: regex + vector parallel", "AI query expansion via Claude Haiku", "NUCC code dedup and classification aggregation"],
    "Code/ConversationalUX/FindCareChat/backend/domain/shared/consent/consent_service.py": ["Two-tier HIPAA consent workflow", "Conversation summarization via Haiku", "De-identification per HIPAA Safe Harbor"],
    "Code/ConversationalUX/FindCareChat/backend/domain/shared/content/about_service.py": ["Skip Snow professional context serving", "ChatHealthy.ai business plan delivery", "Markdown link preservation for contact info"],
    "Code/ConversationalUX/FindCareChat/backend/domain/shared/lead_capture/lead_service.py": ["Lead record creation with consent tiers", "Duplicate email detection", "ConsentService integration for Tier 2"],
    "Code/ConversationalUX/FindCareChat/backend/domain/shared/safety/safety_service.py": ["Dual-trigger emergency detection (keyword + GPT-4.1-mini)", "IP-based session locking (1hr expiration)", "Admin unlock with secret key", "Emergency audit trail"],
    "Code/ConversationalUX/FindCareChat/backend/domain/shared/unknowns/unknown_question_service.py": ["Three-class question classification", "Template responses with consent flow", "De-identification before recording"],
    "Code/ConversationalUX/FindCareChat/backend/infrastructure/debug_logger.py": ["Chat metadata persistence to MongoDB", "Error-triggered de-identification", "Token usage tracking"],
    "Code/ConversationalUX/FindCareChat/backend/infrastructure/embeddings/embedding_client.py": ["Dual-model embeddings (large + small)", "AI query expansion via Claude Haiku", "Lazy OpenAI client initialization"],
    "Code/ConversationalUX/FindCareChat/backend/main.py": ["FastAPI host adapter (329 lines)", "Claude Sonnet 4.6 agentic tool-use loop", "Service wiring for all domain classes", "PromptSystemMaker integration"],
    "Code/ConversationalUX/FindCareChat/backend/url_guardian.py": ["3-stage URL validation (HEAD, AI verify, Google correction)", "Markdown link defanging", "Batch concurrent validation", "TTL-based URL cache"],
    "Code/ConversationalUX/FindCareChat/frontend/src/components/ChatWindow.tsx": ["Chat UI with message history", "45s timeout modal with continue/abandon", "Environment banner (dev/qa/local)", "AbortController for request cancellation", "Session history filtering through safety unlock"],
    "Code/ConversationalUX/FindCareChat/frontend/src/components/MessageBubble.tsx": ["Markdown rendering (react-markdown + GFM)", "HTML sanitization", "Error message styling", "Token/timing metadata footer"],
    "Code/Shared/ChatHealthyMongoUtilities.py": ["MongoDB connection lifecycle management", "Ping validation per access", "Context manager support", "commit() write utility"],
    "Code/Shared/prompt_system_maker.py": ["System prompt assembly from brain artifacts", "Tool definition loading (Anthropic schema)", "Emergency keyword registry", "Brain collection and record reading", "ME context loading"],
    "Code/Shared/brain_runner.py": ["GPT assignment orchestration", "Static system prompt from brain", "Context fetching + Machine Brain search", "Usage logging to cost_guard"],
    "Code/Shared/brain_loop.py": ["Claude-GPT autonomous review loop", "Assignment queue management", "Gate decision logic", "UAT scenario execution"],
    "Code/Shared/brain_auth.py": ["Bearer token auth for Brain API", "Agent identity resolution", "Scope-based access control"],
    "Code/Shared/cost_guard.py": ["Token usage tracking per call", "Model-specific pricing", "Budget enforcement (daily/monthly)"],
    "Code/Shared/machine_brain.py": ["Voyage AI vector embedding (1024 dim)", "MongoDB Atlas vector search", "Decision persistence with ADR tagging", "Keyword fallback search"],
    "Code/Shared/backlog.py": ["Product backlog management", "Feature/story hierarchy", "Status tracking", "Brain assignment linkage"],
    "Code/Shared/ops/uat_report.py": ["UAT feature matrix (18 features)", "Bug/enhancement classification (PE, DB, LG, MS, UX, CF, IF)", "MongoDB UAT status reading", "Clean-start welcome generation"],
    "Code/Shared/ops/bump_build.py": ["Build counter increment in MongoDB", "Environment-specific tracking"],
    "Code/Shared/ops/sync_to_mongo.py": ["Project file sync to admin.project_files", "Text/binary classification", "Manifest snapshot generation", "Artifact type classification"],
    "Code/Shared/ops/promote_data.py": ["Batch data promotion between environments", "PublicHealthData copying", "Dry-run capability"],
    "Code/DataPipelines/function_app.py": ["Azure Durable Functions orchestration", "Provider load pipeline", "County enrichment pipeline", "Cluster warming/cooling"],
    "Code/DataPipelines/gpt_reader.py": ["Read-only MongoDB broker for GPT", "Bearer token auth", "500KB response cap", "100-doc query limit", "Audit logging"],
    "Code/DataPipelines/embedding_worker.py": ["Distributed provider embedding (text-embedding-3-large)", "100-doc batch processing", "Rate limit backoff", "Cursor resumption"],
    "Code/DataPipelines/atlas_cluster_manager.py": ["MongoDB Atlas cluster scaling via API", "Pause/resume for cost optimization", "State polling with IDLE wait"],
    "Code/DataPipelines/auth.py": ["Bearer token validation for Azure Functions", "Token map JSON parsing"],
    "Code/logging_config.py": ["Tee stream for stdout/stderr to log file", "Environment-based log path"],
}

# Update manifest
with open("brain/machine_artifacts/content/project_manifest.json", "r", encoding="utf-8") as f:
    manifest = json.load(f)

updated = 0
for entry in manifest:
    pp = entry.get("project_path", "")
    if pp in CAPABILITIES:
        entry["capabilities"] = CAPABILITIES[pp]
        updated += 1

with open("brain/machine_artifacts/content/project_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)

print(f"Updated {updated} manifest entries with capabilities")
