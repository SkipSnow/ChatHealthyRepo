# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Claude Code PreToolUse Guard — governs ALL tool calls.
#
# Architecture:
#   - Hook in .claude/settings.json calls this script for Bash, Edit, Write
#   - This script reads tool_input from stdin (JSON)
#   - ALLOWLIST approach: only explicitly permitted operations pass
#   - Exit 0 = allow, exit code 2 = block (Claude Code convention)
#
# Governance chain: brain (disk) → guard → hook → Claude
#
# v4-001C: Pipeline code must run on Azure Functions, not locally.
# DR-009: Kill processes only via kill_zombies.py.
# DR-026: HF Spaces only via create_hf_space.py / delete_hf_space.py.

import json
import os
import re
import sys

# ── Allowed file extensions for Edit/Write ─────────────────────────────────
ALLOWED_EXTENSIONS = {
    ".py", ".yaml", ".yml", ".json", ".md", ".html", ".htm",
    ".tsx", ".tsx", ".ts", ".js", ".jsx", ".css", ".scss",
    ".env", ".toml", ".cfg", ".ini", ".txt", ".csv",
    ".sh", ".bat", ".ps1",
    ".gitignore", ".dockerignore",
    ".svg", ".xml",
}

# Files with no extension that are allowed
ALLOWED_FILENAMES = {
    "Dockerfile", "Caddyfile", "Makefile", "Procfile",
    ".gitignore", ".dockerignore", ".env", ".env.local", ".env.example",
}

# Root-level files that are allowed (not under any subdirectory)
ALLOWED_ROOT_FILES = {
    "CLAUDE.md", "ROADMAP.md", "README.md", ".gitignore",
}

# ── Allowed directories for Edit/Write ─────────────────────────────────────
# Relative to project root. Files must be under one of these.
ALLOWED_DIRECTORIES = {
    "Code/",
    "brain/",
    "Website/",
    ".claude/",
    ".github/",
    "docs/",
    "Analysis/",
}

# ── Bash allowlist — read-only and safe operations ─────────────────────────
BASH_ALLOWED = [
    # Git read operations
    r"^git\s+(status|log|diff|branch|show|remote|tag|stash list|rev-parse|ls-files)",
    # Git write operations (committing code is core job)
    r"^git\s+(add|commit|checkout|switch|merge|rebase|stash|pull|fetch)",
    # Git push requires guard approval in future — allow for now
    r"^git\s+push",
    # Python — tests, linting, type checking
    r"^python\s+-m\s+(pytest|mypy|flake8|black|isort|pylint|ruff)",
    r"^python\s+-m\s+playwright",
    # Python — reading/querying (not executing pipeline code)
    r"^python\s+-c\s+[\"'].*?(print|json\.load|json\.dump|count_documents|find_one|find\(|list_collection|list_database|distinct|estimated_document_count|aggregate)",
    # Node/npm — frontend dev
    r"^(npm|npx|node)\s+(run|install|ci|test|build|dev|start)",
    r"^(npm|npx)\s+",
    # Read-only shell commands
    r"^(ls|dir|pwd|echo|cat|head|tail|wc|sort|uniq|diff|file|stat|type|which|where|hostname)\s",
    r"^(curl|wget)\s+.*-s",  # silent fetch
    r"^curl\s",
    # Process inspection (not killing)
    r"^(ps|top|tasklist|netstat)\s",
    r"^(tasklist|netstat)",
    # File operations within project
    r"^(mkdir|cp|mv|rm)\s",
    # Caddy, dev servers
    r"^.*caddy.*\s+(run|start|stop|reload)",
    r"^.*uvicorn\s",
    # Pip
    r"^pip\s+(install|list|show|freeze)",
    # Approved ops scripts
    r"create_hf_space\.py",
    r"delete_hf_space\.py",
    r"kill_zombies\.py",
    r"bump_build\.py",
    # Timeout wrapper
    r"^timeout\s+\d+\s+python\s+-c",
    # Background task output reading
    r"^cat\s+C:/Users",
    # Playwright install
    r"^python\s+-m\s+playwright\s+install",
    # Code editor
    r"^code\s+",
]

# ── Bash blocklist — these are ALWAYS blocked regardless of allowlist ──────
BASH_BLOCKED = [
    # v4-001C: No direct pipeline execution on local machine
    r"python\s+.*Code[/\\]DataPipelines[/\\](?!tests[/\\])",
    r"cd\s+.*Code[/\\]DataPipelines\s*&&\s*python\s+(?!.*pytest)",
    # DR-009: No direct process killing
    r"^taskkill\s",
    r"\bkill\s+[-\d\$]",
    # DR-026: No direct HF API manipulation
    r"python\s+-c.*create_repo.*space",
    r"python\s+-c.*delete_repo.*space",
    r"python\s+-c.*HfApi\(\)\.(?!space_info)",
    # Destructive git operations need explicit approval
    r"git\s+(reset\s+--hard|clean\s+-f|push\s+--force)",
    # No modifying system files
    r"(rm|del)\s+.*(/etc/|/usr/|C:\\Windows)",
    # No direct MongoDB writes via shell
    r"mongosh|mongo\s+.*--eval.*\.(insert|update|delete|drop|remove)",
]


def _check_bash(command: str) -> dict:
    """Check if a Bash command is allowed."""
    command = command.strip()

    # Empty command — allow
    if not command:
        return {"allow": True}

    # Strip leading cd && chains — check the actual command
    # But also check the cd target
    parts = command.split("&&")
    effective_cmd = parts[-1].strip() if parts else command

    # Check blocklist FIRST — these always block
    for pattern in BASH_BLOCKED:
        if re.search(pattern, command, re.IGNORECASE):
            return {
                "allow": False,
                "reason": f"BLOCKED: {pattern}",
            }

    # Check allowlist — must match at least one
    for pattern in BASH_ALLOWED:
        if re.search(pattern, effective_cmd, re.IGNORECASE):
            return {"allow": True}
        # Also check the full command for patterns that can appear anywhere
        if re.search(pattern, command, re.IGNORECASE):
            return {"allow": True}

    return {
        "allow": False,
        "reason": f"Command not in allowlist: {effective_cmd[:100]}",
    }


def _check_file_path(file_path: str) -> dict:
    """Check if a file path is allowed for Edit/Write."""
    if not file_path:
        return {"allow": False, "reason": "No file path provided"}

    # Normalize path
    normalized = file_path.replace("\\", "/")

    # Check filename
    basename = os.path.basename(normalized)
    if basename in ALLOWED_FILENAMES:
        return {"allow": True}

    # Check extension
    _, ext = os.path.splitext(basename)
    if ext and ext.lower() not in ALLOWED_EXTENSIONS:
        return {
            "allow": False,
            "reason": f"File extension '{ext}' not in allowed list",
        }

    # Check directory — file must be under an allowed directory
    # Strip project root prefix if present
    project_markers = ["chatHealthy/findCare/", "chatHealthy\\findCare\\"]
    rel_path = normalized
    for marker in project_markers:
        idx = normalized.find(marker)
        if idx >= 0:
            rel_path = normalized[idx + len(marker):]
            break

    # Root-level files — explicitly allowed
    if "/" not in rel_path and rel_path in ALLOWED_ROOT_FILES:
        return {"allow": True}

    # Anthropic/Claude config files — always allowed
    if ".claude/" in rel_path or ".claude\\" in rel_path:
        return {"allow": True}

    # Memory files — always allowed
    if "memory/" in rel_path:
        return {"allow": True}

    # Check against allowed directories
    for allowed_dir in ALLOWED_DIRECTORIES:
        if rel_path.startswith(allowed_dir):
            return {"allow": True}

    return {
        "allow": False,
        "reason": f"File not in allowed directory: {rel_path}",
    }


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Can't read input — block to be safe
        sys.exit(2)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # ── Bash ───────────────────────────────────────────────────────────────
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        result = _check_bash(command)
        if not result["allow"]:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.get("reason", "Not allowed"),
                }
            }
            json.dump(output, sys.stdout)
            sys.exit(2)
        sys.exit(0)

    # ── Edit ───────────────────────────────────────────────────────────────
    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        result = _check_file_path(file_path)
        if not result["allow"]:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.get("reason", "Not allowed"),
                }
            }
            json.dump(output, sys.stdout)
            sys.exit(2)
        sys.exit(0)

    # ── Write ──────────────────────────────────────────────────────────────
    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        result = _check_file_path(file_path)
        if not result["allow"]:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.get("reason", "Not allowed"),
                }
            }
            json.dump(output, sys.stdout)
            sys.exit(2)
        sys.exit(0)

    # Unknown tool — allow (Read, Glob, Grep are read-only)
    sys.exit(0)


if __name__ == "__main__":
    main()
