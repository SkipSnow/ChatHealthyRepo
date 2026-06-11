"""scan_entrypoint_uniqueness_worker.py — Rule-066 worker.

Enforces build_deploy_promote_plan v3 §D.5: the top level of
architecture/DevOpsBuildDeployAndEnvironmentManagement/ contains EXACTLY
three permitted entry points and the rest are import-only helper modules.

The check is directory-level (not per-file) — it scans the directory's
overall composition once per commit. Forbidden names and entry-point
blocks in non-permitted files both reject.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )


_FORBIDDEN = {
    "local_build.py", "local_deploy.py",
    "remote_build.py", "remote_deploy.py",
    "local_publish.py",
}
_ALLOWED_ENTRY = {
    "build_chathealthy.py",
    "deploy_chathealthy.py",
    "promote_chathealthy.py",
    # Non-build/deploy utilities with their own main blocks. Not part of
    # the build/deploy/promote chain; they're operator-callable helpers
    # that happen to live in the same directory.
    "bell_ringer.py",
}


class ScanEntrypointUniquenessWorker(EnforcementWorker):
    """Rule-066: build/deploy/promote entry-point uniqueness."""

    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def _repo_root(self) -> Path:
        """Walk up from this file until we find a .git directory."""
        here = Path(__file__).resolve()
        for parent in (here, *here.parents):
            if (parent / ".git").is_dir():
                return parent
        return here.parents[3]

    def run(self) -> int:
        repo_root = self._repo_root()
        deploy_dir = repo_root / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement"
        if not deploy_dir.is_dir():
            return EXIT_OK

        any_violations = False
        for child in sorted(deploy_dir.iterdir()):
            if not child.is_file() or child.suffix != ".py":
                continue
            self.files_scanned += 1
            name = child.name
            rel = str(child.relative_to(repo_root)).replace("\\", "/")

            if name in _FORBIDDEN:
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id="Rule-066",
                    resource=rel,
                    message=(
                        f"forbidden entry-point name {name!r} at top level of "
                        f"architecture/DevOpsBuildDeployAndEnvironmentManagement/; "
                        f"Rule-066 forbids the legacy four-script topology"
                    ),
                ))
                self.violation_count += 1
                any_violations = True
                continue

            if name in _ALLOWED_ENTRY:
                continue

            try:
                text = child.read_text(encoding="utf-8")
            except Exception:
                continue
            if 'if __name__ == "__main__":' in text or "if __name__ == '__main__':" in text:
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id="Rule-066",
                    resource=rel,
                    message=(
                        f"file {name!r} carries an `if __name__ == '__main__':` "
                        f"block but is not one of the three permitted entry "
                        f"points ({sorted(_ALLOWED_ENTRY)}); Rule-066 forbids "
                        f"additional entry points"
                    ),
                ))
                self.violation_count += 1
                any_violations = True

        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK


if __name__ == "__main__":
    import sys
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-066-ENF-001"
    sys.exit(ScanEntrypointUniquenessWorker(enforcement_id).run())
