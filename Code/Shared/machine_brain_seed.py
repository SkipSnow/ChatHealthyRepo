"""
Bootstrap Machine Brain with the initial 11 ADR decisions.

Run once per environment:
    ENV_PREFIX=dev MONGODB_CONNECTION_STRING=... python machine_brain_seed.py

ADR: ADR-0007
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from machine_brain import store_decision, list_all_decisions

DECISIONS = [
    {
        "adr_id": "ADR-0001",
        "topic": "system architecture",
        "decision": "Three-application architecture: Static Website, Conversational UX, Data Pipelines.",
        "rationale": "Strict separation allows independent deployment, scaling, and maintenance. Prevents coupling between user-facing UX and heavy data ingestion.",
        "risk": "Low",
        "created_by": "GPT",
        "constraints": [
            "App 1 embeds App 2 via iframe only",
            "App 2 never writes to PublicHealthData",
            "App 3 owns all PublicHealthData writes",
            "App 2 to App 3 via HTTP POST /api/Router with Bearer token only",
        ],
        "components": ["Website", "FindCareChat", "DataPipelines"],
        "decision_type": "architectural",
    },
    {
        "adr_id": "ADR-0002",
        "topic": "frontend hosting",
        "decision": "All frontend hosted on Cloudflare Pages. Azure Static Web Apps will not be used.",
        "rationale": "Cloudflare Pages is free. Azure SWA is paid with no material benefit at current scale.",
        "risk": "Low",
        "created_by": "Skip",
        "constraints": [
            "No Azure SWA for any frontend component",
            "VITE_API_URL injected at build time per environment",
        ],
        "components": ["Website", "FindCareChat frontend"],
        "decision_type": "architectural",
    },
    {
        "adr_id": "ADR-0003",
        "topic": "chat backend FastAPI HuggingFace port",
        "decision": "Port HuggingFace Gradio app to FastAPI on Azure App Service B1.",
        "rationale": "Gradio too brittle for production. FastAPI gives full API control, streaming, safety gating, observability.",
        "risk": "Moderate",
        "created_by": "Claude",
        "constraints": [
            "HuggingFace space stays live until React+FastAPI is smoke-tested",
            "Cutover via chat-url.txt only — no code change required",
            "All app.py logic must be ported: safety gate, tools, Me class, HIPAA consent",
        ],
        "components": ["FindCareChat backend"],
        "decision_type": "architectural",
    },
    {
        "adr_id": "ADR-0004",
        "topic": "environment routing database blob",
        "decision": "Single ENV_PREFIX variable (dev/qa/prod) controls all database and blob routing.",
        "rationale": "One variable, everything follows. No code changes between environments.",
        "risk": "Low",
        "created_by": "Claude",
        "constraints": [
            "Default value: dev",
            "Database: {ENV_PREFIX}_FindCare, {ENV_PREFIX}_PublicHealthData, {ENV_PREFIX}_MachineBrain",
            "Blob: {ENV_PREFIX}-{containerName}",
        ],
        "components": ["FindCareChat backend", "DataPipelines"],
        "decision_type": "operational",
    },
    {
        "adr_id": "ADR-0005",
        "topic": "iframe URL injection chat-url",
        "decision": "Website/chat-url.txt committed per branch, injected at Cloudflare Pages build time via sed.",
        "rationale": "Engineers control the build. One-line git commit changes iframe target per environment.",
        "risk": "Low",
        "created_by": "Claude",
        "constraints": [
            "Build command: CHAT_URL=$(cat chat-url.txt) && sed -i 's|%%CHAT_URL%%|$CHAT_URL|g' index.html",
            "chat-url.txt must exist on every deploying branch",
        ],
        "components": ["Website"],
        "decision_type": "operational",
    },
    {
        "adr_id": "ADR-0006",
        "topic": "MongoDB clusters frontend pipeline",
        "decision": "Two MongoDB clusters: ChatHealthyFrontEndCluster (user-facing) and ChatHealthyPipelineCluster (ingestion).",
        "rationale": "Pipeline load spikes must not affect UX latency. Separation protects user-facing queries.",
        "risk": "Low",
        "created_by": "Skip",
        "constraints": [
            "App 2 reads only from ChatHealthyFrontEndCluster",
            "App 3 writes to ChatHealthyPipelineCluster",
            "copy_to_frontend.py handles cross-cluster data promotion",
        ],
        "components": ["FindCareChat backend", "DataPipelines"],
        "decision_type": "architectural",
    },
    {
        "adr_id": "ADR-0007",
        "topic": "Machine Brain persistent memory architecture",
        "decision": "Machine Brain is a MongoDB database storing architectural decisions, patterns, and knowledge.",
        "rationale": "AI agents lose context between sessions. Machine Brain provides persistent memory. Also a sellable product component.",
        "risk": "Low",
        "created_by": "GPT",
        "constraints": [
            "Claude MUST query before any non-trivial implementation",
            "GPT writes after architectural decisions",
            "Required API: get_decisions(topic) and store_decision(decision)",
            "If Machine Brain is not queried, it does not exist",
        ],
        "components": ["Code/Shared/machine_brain.py"],
        "decision_type": "architectural",
    },
    {
        "adr_id": "ADR-0008",
        "topic": "git branch naming main master",
        "decision": "Production branch is named main, not master.",
        "rationale": "GitHub standard since 2020. Renamed 2026-03-25 before framework_02 was finalized. No operational impact.",
        "risk": "Low",
        "created_by": "Claude",
        "constraints": [
            "All GitHub Actions workflows reference main",
            "GPT to acknowledge deviation from original framework_02 spec",
        ],
        "components": ["All"],
        "decision_type": "operational",
    },
    {
        "adr_id": "ADR-0009",
        "topic": "persistence PERSIST_MODE HIPAA debug",
        "decision": "PERSIST_MODE=debug in dev/QA (full logs), PERSIST_MODE=hipaa in prod (consent gate).",
        "rationale": "Dev/QA need full conversation logs for debugging. Prod is a healthcare system requiring explicit patient consent.",
        "risk": "High",
        "created_by": "Skip",
        "constraints": [
            "PERSIST_MODE=debug must NEVER be set in prod",
            "Safety incidents always persisted regardless of PERSIST_MODE",
        ],
        "components": ["FindCareChat backend"],
        "decision_type": "compliance",
    },
    {
        "adr_id": "ADR-0010",
        "topic": "safety incident logging PHI audit",
        "decision": "Safety incidents logged with IP + timestamp only pending legal review.",
        "rationale": "Minimum data limits PHI exposure while maintaining audit trail. Full requirements pending legal.",
        "risk": "High",
        "created_by": "Skip",
        "constraints": [
            "No consent gate on safety incident logging",
            "Legal review required before adding fields in prod",
            "Stored in {ENV_PREFIX}_FindCare.safety_incidents",
        ],
        "components": ["FindCareChat backend"],
        "decision_type": "compliance",
    },
    {
        "adr_id": "ADR-0011",
        "topic": "git branch naming dev develop integration",
        "decision": "Integration branch retained as dev, not develop.",
        "rationale": "dev already wired to Cloudflare Pages chathealthy-dev project. No technical benefit to renaming.",
        "risk": "Low",
        "created_by": "Claude",
        "constraints": [
            "Cloudflare Pages chathealthy-dev tracks dev branch",
            "GPT to acknowledge deviation from framework_02 naming convention",
        ],
        "components": ["All"],
        "decision_type": "operational",
    },
]


def seed():
    print(f"[MachineBrain] Seeding {len(DECISIONS)} decisions into {os.getenv('ENV_PREFIX', 'dev')}_MachineBrain...")
    success = 0
    for d in DECISIONS:
        ok = store_decision(
            topic=d["topic"],
            decision=d["decision"],
            rationale=d["rationale"],
            risk=d["risk"],
            created_by=d["created_by"],
            adr_id=d.get("adr_id"),
            constraints=d.get("constraints"),
            components=d.get("components"),
            decision_type=d.get("decision_type", "architectural"),
        )
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {d['adr_id']} — {d['topic']}")
        if ok:
            success += 1

    print(f"\n[MachineBrain] Seeded {success}/{len(DECISIONS)} decisions.")
    all_decisions = list_all_decisions()
    print(f"[MachineBrain] Total records in DB: {len(all_decisions)}")


if __name__ == "__main__":
    seed()
