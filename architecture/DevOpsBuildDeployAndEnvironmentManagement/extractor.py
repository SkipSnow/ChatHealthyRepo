"""Write `embedded_content` bytes from a TargetRecord/Collection back
out to the on-disk source_location.

The Extractor is the inverse of the Builder. Given a record (or every
record in a collection), it writes each `files[].embedded_content` to
`repo_root / files[].source_location` verbatim. Binary content is
expected to have been base64-encoded with the `__base64__:` prefix by
the Builder; the Extractor decodes that prefix back to raw bytes.

Fail-hard: any I/O error or malformed base64 raises immediately.
No retry, no recovery.
"""
from __future__ import annotations

import base64
from pathlib import Path

from target_record import DeploymentCollection, FileComposition, TargetRecord


_BASE64_PREFIX: str = "__base64__:"



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


class Extractor:
    def materialize(self, record: TargetRecord, repo_root: Path) -> None:
        for f in record.files:
            self._materialize_one(f, repo_root)

    def materialize_collection(
        self, coll: DeploymentCollection, repo_root: Path
    ) -> None:
        for record in coll:
            self.materialize(record, repo_root)

    @staticmethod
    def _materialize_one(f: FileComposition, repo_root: Path) -> None:
        # Only managed files have JSON-owned bytes. Referenced files
        # are skipped here — the deploy machinery copies them from the
        # origin/<branch> tree at deploy time, not from this artifact.
        if f.disposition != "managed":
            return
        if f.embedded_content is None:
            raise _ch_exc()(
            mode="value_error",
            component="extractor",
            message=f"managed file {f.source_location!r} missing embedded_content")
        out_path = repo_root / f.source_location
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if f.embedded_content.startswith(_BASE64_PREFIX):
            payload = f.embedded_content[len(_BASE64_PREFIX) :]
            raw = base64.b64decode(payload, validate=True)
            out_path.write_bytes(raw)
        else:
            out_path.write_bytes(f.embedded_content.encode("utf-8"))
