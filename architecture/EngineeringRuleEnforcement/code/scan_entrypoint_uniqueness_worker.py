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
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
import sys

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

    _DEPLOY_DIR = "architecture/DevOpsBuildDeployAndEnvironmentManagement"

    def _candidates(self) -> list[Path]:
        """Top-level .py files of the deploy directory, out of the driver's array.

        This worker owns no git knowledge either. It used to walk the
        directory on disk, which answers a different question than the one a
        commit asks: the disk holds files the commit does not publish, and a
        commit publishes files by path rather than by whatever happens to be
        sitting in a directory.
        """
        repo_root = self._repo_root()
        out: list[Path] = []
        for rel in self.files:
            posix = rel.replace("\\", "/")
            if not posix.startswith(f"{self._DEPLOY_DIR}/"):
                continue
            remainder = posix[len(self._DEPLOY_DIR) + 1:]
            if "/" in remainder or not remainder.endswith(".py"):
                continue
            out.append(repo_root / posix)
        return sorted(out)

    def run(self) -> int:
        repo_root = self._repo_root()

        any_violations = False
        for child in self._candidates():
            if not child.is_file():
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


def main() -> int:
    """Drive the program and report its status.

    The exit lives here because this is the function the guard
    calls, and a process reports its outcome by exit code.
    """
    import sys
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-008"
    return ScanEntrypointUniquenessWorker(enforcement_id).run()


if __name__ == "__main__":
    sys.exit(main())
