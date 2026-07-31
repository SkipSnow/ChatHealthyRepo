# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""PipelineConfig loader.

Reads brain/machine_artifacts/content/pipeline_config.json and returns
the single record whose `env` matches the caller's environment.

The brain JSON is the single source of truth (operator directive
2026-07-31: pipeline metadata belongs in the brain, not in Mongo, not
in code). Structure:

    {
      "$schema": ".../ChatHealthyPipelineConfigSchema.json",
      "pipeline_config": [
        {"env": "dev",  "batch_limits": {...}, ...},
        {"env": "qa",   ...},
        {"env": "prod", ...}
      ]
    }

Ships into the pipeline Docker image via a COPY in
pipeline/deploy/Dockerfile.control so the runtime reads it from disk;
no network fetch, no Mongo round-trip, no drift between commit and
what the pipeline sees.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import json
from pathlib import Path
from typing import Any

_log = ChatHealthyLoggingService()

# The brain JSON path resolution: same relative structure in-repo and
# inside the Docker image (both /app/brain/machine_artifacts/content/
# and <repo>/brain/machine_artifacts/content/ walk from pipeline/Code
# via parents[2]).
_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "brain" / "machine_artifacts" / "content" / "pipeline_config.json"
)


def load_pipeline_config(mongo_client=None, env_prefix: str = "dev") -> dict[str, Any]:
    """Return the pipeline_config record for env_prefix.

    mongo_client is retained as a parameter for call-site
    compatibility during the config-to-brain migration; it is IGNORED
    (Mongo is no longer the source of truth). Callers may remove the
    argument at their convenience.

    Raises ChatHealthyException on missing file, malformed JSON, or
    absent env record. No silent fallback -- if the config file is
    missing or wrong the pipeline must fail loud."""
    if not _CONFIG_PATH.is_file():
        raise ChatHealthyException(
            mode="pipeline_config_missing",
            message=(
                f"pipeline_config: brain file not present at "
                f"{_CONFIG_PATH}. Rebuild or repair the Docker image so "
                f"COPY brain/machine_artifacts/content/pipeline_config.json "
                f"lands the file."
            ),
            path=str(_CONFIG_PATH),
        )
    try:
        payload = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatHealthyException(
            mode="pipeline_config_malformed_json",
            message=(
                f"pipeline_config: {_CONFIG_PATH} is not valid JSON: {exc}"
            ),
            path=str(_CONFIG_PATH),
        ) from exc
    records = payload.get("pipeline_config") or []
    for rec in records:
        if isinstance(rec, dict) and rec.get("env") == env_prefix:
            return dict(rec)
    envs_present = [
        r.get("env") for r in records if isinstance(r, dict) and r.get("env")
    ]
    raise ChatHealthyException(
        mode="pipeline_config_env_not_found",
        message=(
            f"pipeline_config: no pipeline_config[] entry for env "
            f"{env_prefix!r}. Envs present in {_CONFIG_PATH}: {envs_present}"
        ),
        path=str(_CONFIG_PATH),
        env_prefix=env_prefix,
        envs_present=envs_present,
    )


def ensure_pipeline_config(mongo_client=None, env_prefix: str = "dev") -> dict[str, Any]:
    """Shim: load_pipeline_config is now authoritative. Callers previously
    relied on ensure_pipeline_config to upsert defaults into Mongo. That
    write path is gone -- the brain JSON is the only source of truth.
    Retained as a shim so prepare_infrastructure keeps working without
    a same-commit change to callers; downstream commits will replace
    call sites with the plain loader."""
    return load_pipeline_config(mongo_client, env_prefix)
