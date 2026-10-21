# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""pipeline_index_catalog -- run-time application of record-declared indexes.

A mongo_indexes package whose config carries version_source is a DECLARATION:
the deploy never applies it (the collection's versioned name is the run's to
resolve), the pipeline container does, through this module. The declarations
live in deployment_architecture.json; in the container -- which carries no
manifest -- they arrive as the derived pipeline_index_packages.json baked at
image-build time (same bake bracket as pipeline_indexes.json). One source,
one derived copy, same pattern as ensure_provider_indexes_activity.

The caller passes the Mongo client it already holds (the run's pipelineEditor
connection), so no second authentication happens here; the entry's declared
cluster/identity remain the record's statement of who owns the index.
"""
from __future__ import annotations

import json
import pathlib

from chathealthy_lib.exceptions import ChatHealthyException

_CONTENT_DIR = (pathlib.Path(__file__).resolve().parents[2]
                / "brain" / "machine_artifacts" / "content")
_BAKED = "pipeline_index_packages.json"
_MANIFEST = "deployment_architecture.json"


def index_package_config(package_id: str) -> dict:
    """The named mongo_indexes package config, from the baked derived file when
    present (the container) else from the manifest (the repo)."""
    baked = _CONTENT_DIR / _BAKED
    if baked.is_file():
        catalog = json.loads(baked.read_text(encoding="utf-8"))
        cfg = catalog.get(package_id)
        if cfg:
            return cfg
        raise ChatHealthyException(
            mode="config_error", component="pipeline_index_catalog",
            message=(f"baked {_BAKED} carries no package {package_id!r}; the "
                     f"image bake and the record have drifted"))
    manifest = _CONTENT_DIR / _MANIFEST
    if manifest.is_file():
        arch = json.loads(manifest.read_text(encoding="utf-8"))
        for rec in arch.get("DeploymentTargetRecord", []):
            if rec.get("target_id") != "target_atlas_pipeline":
                continue
            for e in rec.get("environments", []):
                for p in (e.get("packages") or []):
                    if (p.get("package_id") == package_id
                            and p.get("kind") == "mongo_indexes"):
                        return p.get("config") or {}
    raise ChatHealthyException(
        mode="file_missing", component="pipeline_index_catalog",
        message=(f"neither the baked {_BAKED} nor a {package_id!r} package on "
                 f"target_atlas_pipeline is present to read the index specs"))


def apply_runtime_index_package(mongo, package_id: str, data_version: int) -> list:
    """Idempotently create the named package's declared indexes on its
    versioned collection (<collection>_v_<data_version>; version_source is the
    marker that the run resolves the name). Returns apply_indexes results."""
    from chathealthy_lib.mongo_indexes import apply_indexes  # noqa: PLC0415
    cfg = index_package_config(package_id)
    if cfg.get("version_source") != "run_data_version":
        raise ChatHealthyException(
            mode="config_error", component="pipeline_index_catalog",
            message=(f"package {package_id!r} carries no "
                     f"version_source='run_data_version'; it is not a run-time "
                     f"index package"))
    coll_name = f"{cfg['collection']}_v_{int(data_version)}"
    coll = mongo[cfg["db"]][coll_name]
    return apply_indexes(coll, cfg["indexes"])
