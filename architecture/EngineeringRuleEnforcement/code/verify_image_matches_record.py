"""Refuse to finish an image whose contents differ from the record.

Design V31 section 4.4.3. deployment_architecture.json declares the
interpreter and the exact package versions target_host_local_devops runs on.
A declaration nothing checks is a comment: the image drifts, the record still
reads correct, and the discrepancy surfaces later as a scan result nobody can
reproduce.

This runs inside the image build, after the installs. It compares what is
actually importable against what the record declares and fails the build on
any difference, so an image that exists is an image the record describes.
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import sys
from pathlib import Path

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

TARGET_ID = "target_host_local_devops"


def declared_runtime(record_path: Path) -> dict:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    target = next((t for t in record["DeploymentTargetRecord"]
                   if t["target_id"] == TARGET_ID), None)
    if target is None:
        raise ChatHealthyException(
            "manifest_incomplete",
            f"{TARGET_ID} is not declared, so there is nothing for this image "
            f"to match")
    runtime = target.get("runtime")
    if not runtime:
        raise ChatHealthyException(
            "manifest_incomplete",
            f"{TARGET_ID} declares no runtime. An image built against a "
            f"target that does not say what it runs on cannot be checked, "
            f"and cannot be rebuilt from the record either.")
    return runtime


def differences(runtime: dict) -> list[str]:
    found: list[str] = []

    running = f"python-{sys.version_info.major}.{sys.version_info.minor}"
    if runtime["interpreter_version"] != running:
        found.append(f"interpreter: record declares "
                     f"{runtime['interpreter_version']}, image is {running}")

    for name, version in (runtime.get("python_packages") or {}).items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            found.append(f"{name}: record declares {version}, not installed")
            continue
        if installed != version:
            found.append(f"{name}: record declares {version}, "
                         f"image has {installed}")
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    record_path = Path(args[0]) if args else Path("/tmp/record.json")

    runtime = declared_runtime(record_path)
    found = differences(runtime)
    if found:
        _log.error("the image does not match the record:\n  "
                   + "\n  ".join(found))
        return 1

    _log.info(f"image matches the record: {runtime['interpreter_version']}, "
              f"{len(runtime.get('python_packages') or {})} packages pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
