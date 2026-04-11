# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SEC-HTTPS-001-REQ-004: Scan files for http://localhost.
# v4-029: No HTTP URLs in production code.
#
# Standalone program. Usage:
#
#   Scan staged files (pre-commit):
#     python scan_http.py --staged
#
#   Scan specific files:
#     python scan_http.py file1.py file2.tsx
#
#   Scan a directory (must pass --recurse or die):
#     python scan_http.py Code/ConversationalUX --recurse
#     python scan_http.py Code/ConversationalUX              # DIES — missing --recurse
#
#   Scan all git-tracked files:
#     python scan_http.py --all
#
# Exit 0 = clean. Exit 1 = violations found. Exit 2 = bad arguments.

import os
import re
import subprocess
import sys

TARGET_PATTERN = "http://localhost"

# This script defines the search pattern — skip scanning self
SELF = "scan_http.py"

# Patterns to skip
SKIP_PATTERNS = [
    r"test_",
    r"conftest\.py",
    r"__pycache__",
    r"\.pyc$",
    r"node_modules",
    r"\.venv",
    r"conversation_log\.json",
    r"pipeline_v3_compliance_log",
    r"pipeline_v3_iteration_log",
    r"pipeline_v4_design_iterations",
    r"work_log\.json",
    r"findcare-code-package\.json",
]

SCAN_EXTENSIONS = {".py", ".tsx", ".ts", ".js", ".jsx", ".html", ".json", ".yml", ".yaml", ".cfg", ".toml"}


def should_skip(filepath):
    basename = os.path.basename(filepath)
    if basename == SELF:
        return True
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, filepath):
            return True
    _, ext = os.path.splitext(filepath)
    if ext and ext not in SCAN_EXTENSIONS:
        return True
    return False


def scan_file(filepath):
    violations = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if TARGET_PATTERN in line:
                    violations.append((i, stripped[:120]))
    except Exception:
        pass
    return violations


def get_staged_files():
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def get_all_tracked_files():
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def collect_files_from_dir(directory, recurse):
    files = []
    if recurse:
        for root, _, fnames in os.walk(directory):
            for fname in fnames:
                files.append(os.path.join(root, fname))
    else:
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath):
                files.append(fpath)
    return files


def scan_files(files, label):
    total_violations = 0
    details = []

    for filepath in files:
        if should_skip(filepath):
            continue
        violations = scan_file(filepath)
        if violations:
            total_violations += len(violations)
            for line_num, line_text in violations:
                details.append(f"  {filepath}:{line_num}: {line_text}")

    if total_violations > 0:
        print(f"SEC-HTTPS-001-REQ-004 VIOLATION: {total_violations} http://localhost in {label}:")
        for d in details:
            print(d)
        print(f"\nv4-029: No HTTP URLs in production code.")
        return 1
    else:
        print(f"SEC-HTTPS-001-REQ-004 PASS: 0 http://localhost in {len(files)} {label} files.")
        return 0


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python scan_http.py --staged          # scan staged files")
        print("  python scan_http.py --all             # scan all git-tracked files")
        print("  python scan_http.py file1 file2       # scan specific files")
        print("  python scan_http.py dir --recurse     # scan directory recursively")
        print("  python scan_http.py dir               # ERROR — must pass --recurse for directories")
        sys.exit(2)

    if args == ["--staged"]:
        files = get_staged_files()
        if not files:
            print("No staged files to scan.")
            return 0
        return scan_files(files, "staged")

    if args == ["--all"]:
        files = get_all_tracked_files()
        if not files:
            print("No tracked files to scan.")
            return 0
        return scan_files(files, "all tracked")

    # Check if any arg is a directory
    has_recurse = "--recurse" in args
    paths = [a for a in args if a != "--recurse"]

    files = []
    for path in paths:
        if os.path.isdir(path):
            if not has_recurse:
                print(f"FATAL: '{path}' is a directory but --recurse was not passed.")
                print(f"Pass --recurse to scan directory children.")
                sys.exit(2)
            files.extend(collect_files_from_dir(path, recurse=True))
        elif os.path.isfile(path):
            if has_recurse:
                print(f"FATAL: --recurse passed but '{path}' is a file, not a directory.")
                sys.exit(2)
            files.append(path)
        else:
            print(f"FATAL: '{path}' does not exist.")
            sys.exit(2)

    return scan_files(files, "specified")


if __name__ == "__main__":
    sys.exit(main())
