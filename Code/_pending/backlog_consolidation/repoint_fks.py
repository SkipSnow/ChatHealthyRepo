"""
Repoint every retired req_id / story_id literal in every git-tracked file
(except agile_backlog.json, which the migrate.py step already edited).

Mapping is the inverse of what migrate.py applied to the backlog itself:
- EPIC-008-F-008-S-002              -> EPIC-008-F-002-S-005
- EPIC-008-F-008-S-002-REQ-B-001    -> EPIC-008-F-002-S-005-REQ-B-002
- EPIC-008-F-008-S-002-REQ-T-001    -> EPIC-008-F-002-S-005-REQ-T-001
- EPIC-008-F-008-S-004              -> EPIC-008-F-002-S-006
- EPIC-008-F-008-S-004-REQ-B-001    -> EPIC-008-F-002-S-006-REQ-B-001
- EPIC-008-F-008-S-004-REQ-B-002    -> EPIC-008-F-002-S-003-REQ-B-001
- EPIC-008-F-008-S-004-REQ-T-001    -> EPIC-008-F-002-S-006-REQ-T-001
- EPIC-008-F-008-S-004-REQ-T-002    -> EPIC-008-F-002-S-006-REQ-T-001
- EPIC-008-F-008-S-004-REQ-T-003    -> EPIC-008-F-002-S-003-REQ-T-001

We also need to drop the *-PYTEST-1 suffix variants. The PYTEST entries on
the new requirements are ORPHAN, so any code/test reference to a retired
PYTEST id should be repointed to the parent req's new id (the test file is
no longer wired to a structured pytest entry).
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.chdir(ROOT)

# Order matters: replace the longest patterns first so substring overlap doesn't
# corrupt longer ids. PYTEST suffixes are longest, then REQ ids, then story ids.
MAPPING = [
    # PYTEST suffixes (treat as references to the parent req — drop pytest suffix)
    ("EPIC-008-F-008-S-002-REQ-B-001-PYTEST-1", "EPIC-008-F-002-S-005-REQ-B-002"),
    ("EPIC-008-F-008-S-002-REQ-T-001-PYTEST-1", "EPIC-008-F-002-S-005-REQ-T-001"),
    ("EPIC-008-F-008-S-004-REQ-B-001-PYTEST-1", "EPIC-008-F-002-S-006-REQ-B-001"),
    # REQ ids
    ("EPIC-008-F-008-S-002-REQ-B-001",          "EPIC-008-F-002-S-005-REQ-B-002"),
    ("EPIC-008-F-008-S-002-REQ-T-001",          "EPIC-008-F-002-S-005-REQ-T-001"),
    ("EPIC-008-F-008-S-004-REQ-B-001",          "EPIC-008-F-002-S-006-REQ-B-001"),
    ("EPIC-008-F-008-S-004-REQ-B-002",          "EPIC-008-F-002-S-003-REQ-B-001"),
    ("EPIC-008-F-008-S-004-REQ-T-001",          "EPIC-008-F-002-S-006-REQ-T-001"),
    ("EPIC-008-F-008-S-004-REQ-T-002",          "EPIC-008-F-002-S-006-REQ-T-001"),
    ("EPIC-008-F-008-S-004-REQ-T-003",          "EPIC-008-F-002-S-003-REQ-T-001"),
    # Story ids
    ("EPIC-008-F-008-S-002",                    "EPIC-008-F-002-S-005"),
    ("EPIC-008-F-008-S-004",                    "EPIC-008-F-002-S-006"),
]

# Skip these: agile_backlog.json (already done by migrate.py), the docx files
# (verified to contain zero retired ids), the migration scripts themselves, and
# git internals.
SKIP_PATHS = {
    "brain/machine_artifacts/content/agile_backlog.json",
    "Code/_pending/backlog_consolidation/migrate.py",
    "Code/_pending/backlog_consolidation/repoint_fks.py",
}

# Treat docx and binary files separately — we already verified docx files
# carry zero retired-id literals.
BINARY_EXTS = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico",
               ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot",
               ".db", ".sqlite", ".pyc"}


def is_binary(path: str) -> bool:
    return Path(path).suffix.lower() in BINARY_EXTS


def repoint_file(path: str) -> tuple[int, list[str]]:
    """Returns (replacement_count, list_of_replacement_descriptions)."""
    with open(path, "rb") as f:
        raw = f.read()
    # Try utf-8 first, then latin-1 fallback (some _pending files have weird bytes)
    try:
        text = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        encoding = "latin-1"

    original = text
    counts = []
    for old, new in MAPPING:
        n = text.count(old)
        if n > 0:
            text = text.replace(old, new)
            counts.append(f"{old} -> {new} ({n}x)")
    if text != original:
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(text)
        return sum(int(c.split("(")[1].split("x")[0]) for c in counts), counts
    return 0, []


def main():
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    files = result.stdout.splitlines()

    log = []
    total_replacements = 0
    files_changed = 0

    for f in files:
        if f in SKIP_PATHS:
            continue
        if is_binary(f):
            continue
        fp = f.replace("/", os.sep)
        if not os.path.exists(fp):
            continue
        try:
            n, descs = repoint_file(fp)
        except Exception as e:
            print(f"ERR {f}: {e}")
            continue
        if n > 0:
            files_changed += 1
            total_replacements += n
            log.append((f, n, descs))

    print(f"\n=== REPOINT SUMMARY ===")
    print(f"Files changed: {files_changed}")
    print(f"Total replacements: {total_replacements}\n")
    for f, n, descs in log:
        print(f"  {f} ({n} replacements):")
        for d in descs:
            print(f"    - {d}")

    # Save log for the Word report
    out = ROOT / "Code" / "_pending" / "backlog_consolidation" / "repoint_log.json"
    import json
    with open(out, "w", encoding="utf-8") as f:
        json.dump([
            {"path": p, "replacements": n, "details": d}
            for (p, n, d) in log
        ], f, indent=2, ensure_ascii=False)
    print(f"\nLog written to {out}")


if __name__ == "__main__":
    main()
