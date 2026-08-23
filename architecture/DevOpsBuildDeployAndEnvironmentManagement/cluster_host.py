"""Where a cluster answers, read from the manifest, for the chain's own use.

The library holds no cluster facts and reads the host off the tags on an
identity's certificate secret. That works for a deployed service and cannot
work for the chain, because the chain is what writes those tags: the build
needs Mongo for the build number, Mongo needs a host, the host comes from a
tag, and only a deploy writes tags. A deadlock, and it is one I created.

The chain is a caller that already knows. It reads
deployment_architecture.json for everything else it does, and the host is
declared there as a literal on the key vault target -- the same declaration
the deploy pushes into the tags. So the chain reads the record directly and
passes the host in, and the runtime keeps reading its tag.

One fact, one declaration, two readers with different bootstrap problems.

Imported-only. No entry point.
"""
from __future__ import annotations

import json
from pathlib import Path

_LITERAL = "literal:"
_HOST_PREFIX = "mongo-host-"


def host_for(cluster: str, repo_root: Path | None = None) -> str:
    """The declared host for a cluster, or "" when the record names none."""
    root = repo_root
    if root is None:
        here = Path(__file__).resolve()
        root = next((p for p in here.parents if (p / ".git").exists()), None)
    if root is None:
        return ""
    manifest = (root / "brain" / "machine_artifacts" / "content"
                / "deployment_architecture.json")
    if not manifest.is_file():
        return ""
    record = json.loads(manifest.read_text(encoding="utf-8"))
    wanted = f"{_HOST_PREFIX}{cluster}"
    for target in record.get("DeploymentTargetRecord", []):
        declared = (target.get("secrets") or {}).get(wanted)
        if isinstance(declared, str) and declared.startswith(_LITERAL):
            return declared[len(_LITERAL):].strip()
    return ""
