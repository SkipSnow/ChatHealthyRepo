# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""technical_model.py — canonical config for ChatHealthy deploy workflows.

This is the ONE source of truth for the inputs that drive the GitHub Actions
deploy workflow YAMLs. Edit here; regenerate; never edit the YAMLs by hand.

The fields below are extracted from the existing four findcare-* workflow
YAMLs and `Code/deploy/remote_deploy.py`. They are the technical-model facts
that the generator templates substitute into the rendered YAML.

No silent fallbacks: every workflow that references a key listed here must
have a value here. Missing values raise KeyError at render time (loud
failure beats silent drift — that's exactly what the prod-website mis-map
taught us on 2026-05-09).
"""
from __future__ import annotations


# ── Org / repo / runtime constants ─────────────────────────────────────────
ORG = "SkipSnow"
REPO = "ChatHealthyRepo"
HF_OWNER = "SkipSnow"
PYTHON_VERSION = "3.12"
NODE_VERSION = 20
GIT_USER_EMAIL = "skip.snow@gmail.com"
GIT_USER_NAME = "SkipSnow"


# ── Envs ───────────────────────────────────────────────────────────────────
# Branch → env mapping. Single source of truth. Used by every workflow that
# fans out per env (the website workflows) AND by the cross-env backend
# workflows for their `if [ "$BRANCH" = "main" ]` style branching.
ENVS = {
    "dev":  {"branch": "dev",  "is_prod": False},
    "qa":   {"branch": "qa",   "is_prod": False},
    "prod": {"branch": "main", "is_prod": True},
}


# ── Cloudflare Pages projects (per env) ────────────────────────────────────
# Note: prod uses `chathealthywebsite` (no hyphen separator) — historical,
# do not "fix" without Skip's explicit approval.
CLOUDFLARE_PAGES_PROJECT = {
    "dev":  "chathealthy-website-dev",
    "qa":   "chathealthy-website-qa",
    "prod": "chathealthywebsite",
}


# ── HF Space base names per backend service ────────────────────────────────
# Space NAME (API form): `${BRANCH}_${BASE}` for non-main, `${BASE}` for main.
# Public URL form: `skipsnow-${env}-${base_lower}.hf.space` for non-prod,
# `skipsnow-${base_lower}.hf.space` for prod.
HF_SPACE_BASE = {
    "findcare": "ChatHealthySpace",
    "evalcare": "EvaluateCareSpace",
    "shared":   "SharedServicesSpace",
}


# ── Smoke test invocation (S-006 master smoke) ─────────────────────────────
SMOKE_TEST_PATH = "Code/deploy/localSmokeTestPyTest.py"
SMOKE_PIP_PACKAGES = (
    "pytest pytest-playwright httpx cryptography psutil "
    "python-dotenv jsonschema anyio"
)


# ── Backend workflow definitions ───────────────────────────────────────────
# Each backend has one cross-env workflow that derives its target HF Space
# from the pushed branch. The differences between findcare/evalcare/shared
# backends are encoded here.
BACKEND_WORKFLOWS = {
    "findcare_backend": {
        "filename": "deploy-findcare-backend.yml",
        "display_name": "Deploy ChatHealthy Backend to HuggingFace",
        "service_key": "findcare",
        "trigger_paths": [
            "Code/ConversationalUX/FindCareChat/backend/**",
            "Code/ConversationalUX/FindCareChat/frontend/**",
            "Code/ConversationalUX/ChatHealthyWhoAmIChat/me/**",
            "Code/Shared/ChatHealthyMongoUtilities.py",
            "Code/Shared/prompt_system_maker.py",
            "brain/machine_artifacts/content/emergency_keywords.json",
            ".github/workflows/deploy-findcare-backend.yml",
        ],
        "needs_react_build": True,
        "needs_lfs": True,
        "signing_key_secret": "FINDCARE_SIGNING_KEY_B64",
        "signing_key_var": "FINDCARE_SIGNING_KEY_PEM",
        "signing_key_file": "Code/Shared/ops/certs/findcare.key",
        "peer_certs": ["findcare", "shared", "evalcare", "ca"],
        "peer_urls": ["evalcare", "shared"],
        "verify_all_peers": True,  # extra step: poll all 3 backends after deploy
    },
    "evalcare_backend": {
        "filename": "deploy-evaluatecare-backend.yml",
        "display_name": "Deploy EvaluateCare Backend to HuggingFace",
        "service_key": "evalcare",
        "trigger_paths": [
            "evaluateCare/Code/**",
            ".github/workflows/deploy-evaluatecare-backend.yml",
        ],
        "needs_react_build": False,
        "needs_lfs": False,
        "signing_key_secret": "EVALCARE_SIGNING_KEY_B64",
        "signing_key_var": "EVALCARE_SIGNING_KEY_PEM",
        "signing_key_file": "Code/Shared/ops/certs/evalcare.key",
        "peer_certs": ["findcare", "evalcare", "ca"],
        "peer_urls": ["findcare", "shared"],
        "verify_all_peers": False,
    },
    "shared_backend": {
        "filename": "deploy-shared-services.yml",
        "display_name": "Deploy Shared Services to HuggingFace",
        "service_key": "shared",
        "trigger_paths": [
            "sharedServices/Code/**",
            ".github/workflows/deploy-shared-services.yml",
        ],
        "needs_react_build": False,
        "needs_lfs": False,
        "signing_key_secret": "SHARED_SIGNING_KEY_B64",
        "signing_key_var": "SHARED_SIGNING_KEY_PEM",
        "signing_key_file": "Code/Shared/ops/certs/shared.key",
        "peer_certs": ["findcare", "shared", "ca"],
        "peer_urls": ["findcare", "evalcare"],
        "verify_all_peers": False,
    },
}


# ── Website workflow definition (one yaml per env) ─────────────────────────
# All three env-mirrored copies share this template; per-env values come from
# ENVS + CLOUDFLARE_PAGES_PROJECT.
WEBSITE_WORKFLOW = {
    "filename_template": "deploy-findcare-website-{env}.yml",
    "display_name_template": "Deploy FindCare Website ({env_title})",
    "trigger_paths_template": [
        "Website/**",
        ".github/workflows/deploy-findcare-website-{env}.yml",
    ],
}


# ── Composite ──────────────────────────────────────────────────────────────
TECHNICAL_MODEL = {
    "org": ORG,
    "repo": REPO,
    "hf_owner": HF_OWNER,
    "python_version": PYTHON_VERSION,
    "node_version": NODE_VERSION,
    "git_user_email": GIT_USER_EMAIL,
    "git_user_name": GIT_USER_NAME,
    "envs": ENVS,
    "cloudflare_pages_project": CLOUDFLARE_PAGES_PROJECT,
    "hf_space_base": HF_SPACE_BASE,
    "smoke_test_path": SMOKE_TEST_PATH,
    "smoke_pip_packages": SMOKE_PIP_PACKAGES,
    "backend_workflows": BACKEND_WORKFLOWS,
    "website_workflow": WEBSITE_WORKFLOW,
}
