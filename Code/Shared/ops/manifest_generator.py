# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ManifestGenerator — produces the complete project manifest.
# Single class, single call, complete output.

import hashlib
import json
import os
import subprocess
from pathlib import Path


class ManifestGenerator:
    """Generates the project manifest: files, hashes, entity types, capabilities.

    Usage:
        gen = ManifestGenerator(project_root="/path/to/findCare")
        gen.generate()
        gen.save()
    """

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
        "Code/Shared/ops/uat_report.py": ["UAT feature matrix (18 features)", "Bug/enhancement classification", "MongoDB UAT status reading", "Clean-start welcome generation"],
        "Code/Shared/ops/bump_build.py": ["Build counter increment in MongoDB", "Environment-specific tracking"],
        "Code/Shared/ops/sync_to_mongo.py": ["Project file sync to admin.project_files", "Text/binary classification", "Manifest snapshot generation"],
        "Code/Shared/ops/promote_data.py": ["Batch data promotion between environments", "PublicHealthData copying", "Dry-run capability"],
        "Code/Shared/ops/manifest_generator.py": ["Complete project manifest generation", "File hashing (SHA-256)", "Entity type classification", "Code capability mapping"],
        "Code/Shared/ops/dev_pipeline.py": ["Dev pipeline orchestration", "Manifest generation step"],
        "Code/DataPipelines/function_app.py": ["Azure Durable Functions orchestration", "Provider load pipeline", "County enrichment pipeline", "Cluster warming/cooling"],
        "Code/DataPipelines/gpt_reader.py": ["Read-only MongoDB broker for GPT", "Bearer token auth", "500KB response cap", "100-doc query limit", "Audit logging"],
        "Code/DataPipelines/embedding_worker.py": ["Distributed provider embedding (text-embedding-3-large)", "100-doc batch processing", "Rate limit backoff"],
        "Code/DataPipelines/atlas_cluster_manager.py": ["MongoDB Atlas cluster scaling via API", "Pause/resume for cost optimization"],
        "Code/DataPipelines/auth.py": ["Bearer token validation for Azure Functions"],
        "Code/logging_config.py": ["Tee stream for stdout/stderr to log file", "Environment-based log path"],
    }

    DIRECTORIES = {
        "brain/": "The Brain — AI-native SDLC operating system. Governance, prompts, plans, assignments, audit trails, manifests.",
        "brain/machine_artifacts/": "Machine-generated Brain artifacts. Collections, code tools, iteration cache.",
        "brain/machine_artifacts/content/": "Brain pseudo-collections. JSON files with records[] and _record_id per record. Source of truth for all Brain data.",
        "brain/machine_artifacts/code/": "Brain tools — scripts that generate PDFs, manifests, and other artifacts.",
        "brain/machine_artifacts/.iteration_cache/": "Epic planning iteration cache. GPT and Claude working artifacts. Cleaned after planning cycle.",
        "brain/BusinessArtifacts/": "Business gate artifacts — QA reports, IP agreements, investor docs, UAT reports.",
        "brain/BusinessArtifacts/diagrams/": "Business architecture diagrams — component, moat, revenue, compliance.",
        "brain/machine_artifacts/content/": "Project manifest — every file cataloged with capabilities and descriptions.",
        "Code/": "All production code. Three applications + shared.",
        "Code/ConversationalUX/": "App 2 — Chat Window. React frontend + FastAPI backend.",
        "Code/ConversationalUX/FindCareChat/": "FindCare chat application. Active development.",
        "Code/ConversationalUX/FindCareChat/backend/": "FastAPI backend — tools, safety, search, consent, LLM orchestration.",
        "Code/ConversationalUX/FindCareChat/backend/domain/": "Domain services — business logic separated by bounded context.",
        "Code/ConversationalUX/FindCareChat/backend/application/": "Application layer — facades, tool router, Pydantic models.",
        "Code/ConversationalUX/FindCareChat/backend/infrastructure/": "Infrastructure — embeddings, debug logging, MongoDB utilities.",
        "Code/ConversationalUX/FindCareChat/frontend/": "React + TypeScript frontend. Chat UI, message rendering.",
        "Code/ConversationalUX/FindCareChat/frontend/src/": "Frontend source — components, styles, app root.",
        "Code/ConversationalUX/FindCareChat/frontend/src/components/": "React components — ChatWindow, MessageBubble.",
        "Code/ConversationalUX/ChatHealthyWhoAmIChat/": "Legacy Gradio chatbot. Active until FindCare replaces it.",
        "Code/DataPipelines/": "App 3 — Data Pipelines. Azure Functions, provider load, county enrichment.",
        "Code/DataPipelines/tests/": "Pipeline tests — integration, SIT runner, GPT SIT.",
        "Code/Shared/": "Shared code — MongoDB utilities, Brain runner, cost guard, prompt maker.",
        "Code/Shared/ops/": "DevOps tools — UAT report, manifest generator, build bump, epic planning runner.",
        "Website/": "App 1 — Static Website. Cloudflare Pages. Design docs, roadmap, architecture.",
        "Website/tests/": "Website tests.",
        "Legal/": "Legal documents — IP license, compliance.",
        ".github/": "GitHub configuration.",
        ".github/workflows/": "CI/CD — deploy pipelines, test workflows, build promotion.",
    }

    def __init__(self, project_root: str = None):
        self._root = project_root or str(Path(__file__).parent.parent.parent.parent)
        self._manifest = []

    def _describe(self, rel_path: str, full_path: str, entity_type: str) -> str:
        """Generate a description for any file. GPT uses this to decide what to fetch."""
        # Known capabilities take priority
        if rel_path in self.CAPABILITIES:
            return "; ".join(self.CAPABILITIES[rel_path])

        # Brain collections — read the collection-level description from JSON
        if entity_type == "collection":
            try:
                with open(full_path, encoding="utf-8") as f:
                    data = json.load(f)
                coll = data.get("collection", "")
                count = data.get("record_count", len(data.get("records", [])))
                # Get first record descriptions as sample
                records = data.get("records", [])
                sample_ids = [r.get("_record_id", "") for r in records[:5] if isinstance(r, dict)]
                return f"Brain collection '{coll}' ({count} records): {', '.join(sample_ids)}"
            except Exception:
                return "Brain collection (unreadable)"

        # HTML design docs — extract title
        if rel_path.startswith("Website/") and rel_path.endswith(".html"):
            try:
                with open(full_path, encoding="utf-8") as f:
                    content = f.read(2000)
                import re
                match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
                if match:
                    return f"Design doc: {match.group(1)}"
            except Exception:
                pass
            return "Website HTML page"

        # GitHub workflows
        if rel_path.startswith(".github/workflows/"):
            name = Path(rel_path).stem
            return f"CI/CD workflow: {name}"

        # Dockerfiles
        if Path(rel_path).name == "Dockerfile":
            return f"Docker build for {Path(rel_path).parent.name}"

        # Requirements/package files
        if Path(rel_path).name in ("requirements.txt", "requirements-pipeline.txt", "package.json"):
            return f"Dependencies for {Path(rel_path).parent.name}"

        # Brain business artifacts
        if rel_path.startswith("brain/BusinessArtifacts/"):
            name = Path(rel_path).stem
            ext = Path(rel_path).suffix
            return f"Business artifact: {name} ({ext})"

        # Brain manifest
        if rel_path.startswith("brain/machine_artifacts/content/"):
            return "Project manifest — file catalog with capabilities"

        # Brain code
        if rel_path.startswith("brain/machine_artifacts/code/"):
            return f"Brain tool: {Path(rel_path).stem}"

        # Config files
        if Path(rel_path).name in (".gitignore", "tsconfig.json", "vite.config.ts", "tailwind.config.js"):
            return f"Config: {Path(rel_path).name}"

        # Test files
        if "/tests/" in rel_path or rel_path.startswith("tests/"):
            return f"Test: {Path(rel_path).stem}"

        # Frontend source
        if rel_path.endswith((".tsx", ".ts")) and "frontend" in rel_path:
            return f"Frontend component: {Path(rel_path).stem}"

        # Python source without capabilities entry
        if rel_path.endswith(".py"):
            return f"Python module: {Path(rel_path).stem}"

        # Legal
        if rel_path.startswith("Legal/"):
            return f"Legal document: {Path(rel_path).stem}"

        # Fallback
        return f"{Path(rel_path).suffix or 'file'} in {Path(rel_path).parent}"

    def generate(self) -> list:
        """Generate the complete manifest from git tracked files."""
        result = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, cwd=self._root
        )
        tracked = result.stdout.strip().split("\n")

        self._manifest = []

        for rel_path in sorted(tracked):
            full = os.path.join(self._root, rel_path)
            if not os.path.isfile(full):
                continue

            # Hash
            try:
                size = os.path.getsize(full)
                with open(full, "rb") as f:
                    content_hash = hashlib.sha256(f.read()).hexdigest()[:16]
            except Exception:
                size, content_hash = 0, ""

            # Entity type
            if rel_path.startswith("brain/machine_artifacts/content/") and rel_path.endswith(".json"):
                entity_type = "collection"
            elif "diagrams/" in rel_path and rel_path.endswith(".png"):
                entity_type = "diagram"
            else:
                entity_type = "file"

            entry = {
                "project_path": rel_path,
                "size": size,
                "content_hash": content_hash,
                "entity_type": entity_type,
            }

            # Capabilities for known code files
            if rel_path in self.CAPABILITIES:
                entry["capabilities"] = self.CAPABILITIES[rel_path]

            # Description for every file — GPT needs this to decide what to read
            entry["description"] = self._describe(rel_path, full, entity_type)

            self._manifest.append(entry)

        # Add directories with descriptions
        for d, desc in self.DIRECTORIES.items():
            self._manifest.append({
                "project_path": d,
                "size": 0,
                "content_hash": "",
                "entity_type": "directory",
                "description": desc,
            })

        return self._manifest

    def save(self, output_path: str = None) -> str:
        """Save manifest to JSON file."""
        if not self._manifest:
            self.generate()
        path = output_path or os.path.join(self._root, "brain", "manifest", "project_manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=2)
        return path

    @property
    def file_count(self) -> int:
        return len([e for e in self._manifest if e["entity_type"] != "directory"])
