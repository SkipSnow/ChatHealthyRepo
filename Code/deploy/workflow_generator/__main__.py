# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""__main__.py — regenerate the four FindCare deploy workflow YAMLs.

Usage:
    python -m Code.deploy.workflow_generator [--check] [--diff]

Flags:
    --check : exit 1 if the on-disk YAMLs differ from the generated output.
              Does NOT write. Intended for CI / pre-commit.
    --diff  : like --check but also print a unified diff for each file
              that differs.

Default (no flags): write each rendered YAML to .github/workflows/.

Requires CHATHEALTHY_PROJECT_ROOT in the environment so generation is
deterministic regardless of cwd.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

from Code.deploy.workflow_generator.technical_model import TECHNICAL_MODEL
from Code.deploy.workflow_generator.workflow_generator import WorkflowGenerator


def _resolve_repo_root() -> Path:
    if "CHATHEALTHY_PROJECT_ROOT" in os.environ:
        return Path(os.environ["CHATHEALTHY_PROJECT_ROOT"]).resolve()
    # Fall back to walking up from this file: workflow_generator/ -> deploy/
    # -> Code/ -> repo root.
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="workflow_generator")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if on-disk YAMLs differ from generated output. "
             "Does not write.",
    )
    ap.add_argument(
        "--diff",
        action="store_true",
        help="Print unified diffs for any files that differ. "
             "Implies --check (does not write).",
    )
    args = ap.parse_args(argv)
    check_mode = args.check or args.diff

    repo_root = _resolve_repo_root()
    gen = WorkflowGenerator(TECHNICAL_MODEL, repo_root=repo_root)
    rendered = gen.render_all()

    workflows_dir = repo_root / ".github" / "workflows"
    differences = []
    for fn, content in rendered.items():
        target = workflows_dir / fn
        # Compare bytes (CRLF-encoded) for byte-identical check; render the
        # diff in LF form for human readability.
        rendered_bytes = WorkflowGenerator._encode(content)
        if not target.exists():
            differences.append((fn, None, content))
            continue
        on_disk_bytes = target.read_bytes()
        if on_disk_bytes != rendered_bytes:
            on_disk = on_disk_bytes.decode("utf-8").replace("\r\n", "\n")
            differences.append((fn, on_disk, content))

    if check_mode:
        if not differences:
            print("OK — all four YAMLs match generator output.")
            return 0
        print(
            f"DRIFT — {len(differences)} workflow file(s) differ "
            f"from generator output:"
        )
        for fn, on_disk, content in differences:
            print(f"  - {fn}")
            if args.diff and on_disk is not None:
                diff = difflib.unified_diff(
                    on_disk.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/.github/workflows/{fn}",
                    tofile=f"b/.github/workflows/{fn}",
                )
                sys.stdout.writelines(diff)
        return 1

    # Write mode.
    written = gen.write_all()
    for fn, path in written.items():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
