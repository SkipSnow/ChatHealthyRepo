# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Upload frozen integration snapshots to pipeline-sources-test/{snapshot_id}/ — LLD §9.3."""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import argparse
import json

import os
import sys

_log = ChatHealthyLoggingService()

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pipeline_env import load_pipeline_env

load_pipeline_env()


def _upload_blob(container: str, blob_name: str, data: bytes) -> None:
    from blob_client import get_blob_service

    cc = get_blob_service().get_container_client(container)
    try:
        cc.create_container()
    except Exception:
        pass
    cc.upload_blob(name=blob_name, data=data, overwrite=True)
    _log.info("uploaded %s/%s (%d bytes)", container, blob_name, len(data))


def ensure_wy_vt_snapshots(snapshot_id: str = "wy-vt-v1", container: str = "provider-data") -> dict:
    """Ensure minimal frozen snapshot blobs exist for WY+VT integration tests."""
    from blob_client import get_blob_service
    from pipeline_integration_snapshot import snapshot_blob_path, SOURCE_KEYS

    # Legacy blob name for usda_rucc; urban_flag.py was retired but the
    # constant lives on here inline for the snapshot copy.
    RUCC_JSON_BLOB_NAME = "rucc.json"

    cc = get_blob_service().get_container_client(container)
    report: dict = {"snapshot_id": snapshot_id, "uploaded": [], "existing": []}

    # NPPES: copy latest npi zip if present
    nppes_blobs = sorted(cc.list_blobs(name_starts_with="npi_"), key=lambda b: b.name, reverse=True)
    if nppes_blobs:
        src = nppes_blobs[0].name
        data = cc.get_blob_client(src).download_blob().readall()
        dest = snapshot_blob_path("nppes-npi", snapshot_id=snapshot_id, ext="zip")
        if not cc.get_blob_client(dest).exists():
            _upload_blob(container, dest, data)
            report["uploaded"].append(dest)
        else:
            report["existing"].append(dest)

    if cc.get_blob_client(RUCC_JSON_BLOB_NAME).exists():
        data = cc.get_blob_client(RUCC_JSON_BLOB_NAME).download_blob().readall()
        dest = snapshot_blob_path("usda-rucc", snapshot_id=snapshot_id, ext="json")
        if not cc.get_blob_client(dest).exists():
            _upload_blob(container, dest, data)
            report["uploaded"].append(dest)
        else:
            report["existing"].append(dest)

    for key in SOURCE_KEYS:
        path = snapshot_blob_path(key, snapshot_id=snapshot_id)
        if cc.get_blob_client(path).exists():
            report["existing"].append(path)

    report["ready"] = bool(report["uploaded"] or report["existing"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload LLD §9.3 integration snapshots")
    parser.add_argument("--snapshot-id", default=os.environ.get("PIPELINE_TEST_SNAPSHOT_ID", "wy-vt-v1"))
    parser.add_argument("--container", default="provider-data")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = ensure_wy_vt_snapshots(args.snapshot_id, args.container)
    if args.json:
        ChatHealthyLoggingService().info(json.dumps(report, indent=2))
    else:
        ChatHealthyLoggingService().info(json.dumps(report, indent=2))
    return 0 if report.get("ready") else 1


if __name__ == "__main__":
    sys.exit(main())
