# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SEC-HTTPS-001-REQ-004: Scan files for insecure HTTP URLs.
# v4-029: No HTTP URLs in production code.
#
# The ONLY allowed http:// pattern is a redirect to index.html on one
# of the 4 named web servers. Everything else is a violation.
#
# Usage:
#   python scan_http.py --staged
#   python scan_http.py --all
#   python scan_http.py file1.py file2.tsx
#   python scan_http.py Code/ConversationalUX --recurse
#
# Exit 0 = clean. Exit 1 = violations. Exit 2 = bad arguments.

import json
import os
import re
import subprocess
import sys

# The pattern we scan for
TARGET = "http://"

# The 4 named web servers that are allowed to redirect index.html from HTTP
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "dev.chathealthy.ai",
    "qa.chathealthy.ai",
    "chathealthy.ai",
]

# Build regex for allowed patterns:
# 1. http://{named host}/ or http://{named host}/index.html — the redirect
# 2. http://www.w3.org/2000/svg — XML namespace required by SVG spec
# 3. http://169.254.169.254 — Azure Instance Metadata Service (always HTTP by spec)
ALLOWED_PATTERNS = [
    re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?(/.*)?$"),  # Local services (sidecar, dev servers)
    re.compile(r"http://(" + "|".join(re.escape(h) for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")) + r")(:\d+)?(/index\.html|/)?$"),
    re.compile(r"http://www\.w3\.org/"),           # XML/SVG namespaces
    re.compile(r"http://169\.254\.169\.254/"),      # Azure IMDS
]

# This file defines TARGET and regex patterns — excuse lines that are definitions
SELF_PATTERN_LINE = "TARGET"

# SEC-HTTPS-001-REQ-004 scope: production code only.
# Exempt from HTTP URL scanning:
#   - All .json files (governance artifacts, config, schemas — not production code)
#   - All pytest files (test_*, conftest.py — DevOps code, not production endpoints)
#   - All business documents (brain/BusinessArtifacts/)
HTTP_EXEMPT_PATTERNS = [
    r"\.json$",                           # All JSON files
    r"test_",                             # Pytest files
    r"conftest\.py",                      # Pytest config
    r"brain/BusinessArtifacts/",          # Business documents
]

# General skip patterns (not scanned by any rule)
SKIP_PATTERNS = [
    r"__pycache__",
    r"\.pyc$",
    r"node_modules",
    r"\.venv",
]

SCAN_EXTENSIONS = {".py", ".tsx", ".ts", ".js", ".jsx", ".html", ".json", ".yml", ".yaml", ".cfg", ".toml"}


def should_skip(filepath):
    """Skip files that no rule should scan."""
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, filepath):
            return True
    _, ext = os.path.splitext(filepath)
    if ext and ext not in SCAN_EXTENSIONS:
        return True
    return False


def is_http_exempt(filepath):
    """SEC-HTTPS-001-REQ-004: HTTP URL rule only applies to production code.
    JSON files, pytests, and business documents are exempt."""
    for pattern in HTTP_EXEMPT_PATTERNS:
        if re.search(pattern, filepath):
            return True
    return False


def extract_http_urls(line):
    """Extract all http:// URLs from a line."""
    return re.findall(r"http://[^\s\"'`,\)}\]>]+", line)


def scan_file(filepath):
    violations = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                # Excuse lines in scan_http.py that define the pattern or regex
                if os.path.basename(filepath) == "scan_http.py":
                    if stripped.startswith(SELF_PATTERN_LINE) and "=" in stripped:
                        continue
                    if "re.compile" in stripped or "re.findall" in stripped:
                        continue
                if TARGET not in line:
                    continue
                # Extract each http:// URL and check against allowed patterns
                urls = extract_http_urls(line)
                for url in urls:
                    allowed = False
                    for pattern in ALLOWED_PATTERNS:
                        if pattern.match(url):
                            allowed = True
                            break
                    if allowed:
                        continue
                    violations.append((i, url, stripped[:120]))
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


# BRAIN-SCHEMA-REQ-001: JSON files exempt from schema validation (third-party owned)
SCHEMA_EXEMPT_PATTERNS = [
    r"\.claude/",                         # Anthropic
    r"\.vscode/",                         # VS Code / Microsoft
    r"package\.json$",                    # npm
    r"package-lock\.json$",              # npm lock
    r"tsconfig\.json$",                   # TypeScript
    r"host\.json$",                       # Azure Functions
    r"appsettings.*\.json$",              # .NET config
    r"launchSettings\.json$",             # .NET launch config
    r"node_modules",
    r"__pycache__",
    r"\.iteration_cache/",               # Gitignored iteration cache
    r"brain/BusinessArtifacts/Audits/",  # Business audit documents (exempt per Boss)
]


def is_schema_exempt(filepath):
    """BRAIN-SCHEMA-REQ-001: Third-party JSON files are exempt from schema validation."""
    for pattern in SCHEMA_EXEMPT_PATTERNS:
        if re.search(pattern, filepath):
            return True
    return False


def scan_json_schema(filepath):
    """BRAIN-SCHEMA-REQ-001: Every ChatHealthy JSON file must have a $schema reference."""
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None  # Arrays and primitives are not required to have $schema
        if "$schema" not in data and "schema_address" not in data:
            return f"Missing $schema field"
        return None
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    except Exception:
        return None


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
    # SEC-HTTPS-001-REQ-004: HTTP URL scan
    http_violations = 0
    http_details = []

    for filepath in files:
        if should_skip(filepath):
            continue
        if not is_http_exempt(filepath):
            violations = scan_file(filepath)
            if violations:
                http_violations += len(violations)
                for line_num, url, line_text in violations:
                    http_details.append(f"  {filepath}:{line_num}: {url}")

    # BRAIN-SCHEMA-REQ-001: JSON schema validation
    schema_violations = 0
    schema_details = []

    for filepath in files:
        if not filepath.endswith(".json"):
            continue
        if is_schema_exempt(filepath):
            continue
        error = scan_json_schema(filepath)
        if error:
            schema_violations += 1
            schema_details.append(f"  {filepath}: {error}")

    # Report HTTP results
    exit_code = 0
    if http_violations > 0:
        print(f"SEC-HTTPS-001-REQ-004 VIOLATION: {http_violations} insecure HTTP URLs in {label}:")
        for d in http_details:
            print(d)
        print(f"\nv4-029: No HTTP URLs in production code.")
        print(f"Allowed: {', '.join(ALLOWED_HOSTS)} for /index.html redirect only.")
        exit_code = 1
    else:
        print(f"SEC-HTTPS-001-REQ-004 PASS: 0 insecure HTTP URLs in {len(files)} {label} files.")

    # Report schema results
    if schema_violations > 0:
        print(f"\nBRAIN-SCHEMA-REQ-001 VIOLATION: {schema_violations} JSON files missing $schema:")
        for d in schema_details:
            print(d)
        print(f"\nEvery ChatHealthy JSON file must reference a schema via $schema or schema_address.")
        exit_code = 1
    else:
        json_count = sum(1 for f in files if f.endswith(".json") and not is_schema_exempt(f))
        if json_count > 0:
            print(f"BRAIN-SCHEMA-REQ-001 PASS: {json_count} JSON files have schema references.")

    return exit_code


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python scan_http.py --staged")
        print("  python scan_http.py --all")
        print("  python scan_http.py file1 file2")
        print("  python scan_http.py dir --recurse")
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

    has_recurse = "--recurse" in args
    paths = [a for a in args if a != "--recurse"]

    files = []
    for path in paths:
        if os.path.isdir(path):
            if not has_recurse:
                print(f"FATAL: '{path}' is a directory but --recurse was not passed.")
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
