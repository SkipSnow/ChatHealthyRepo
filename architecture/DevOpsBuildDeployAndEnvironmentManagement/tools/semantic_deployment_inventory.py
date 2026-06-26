#!/usr/bin/env python3
"""Semantic deployment inventory — code-path proof drives file ownership.

Every file in a target's semantic set MUST have at least one ProofEdge in
code_path_proof.py (build / runtime / deploy / hook / docker / static).
See _oneshots/code_path_proof_<date>.jsonl for the full audit trail.

Usage:
  python architecture/DevOpsBuildDeployAndEnvironmentManagement/semantic_deployment_inventory.py
  python architecture/DevOpsBuildDeployAndEnvironmentManagement/semantic_deployment_inventory.py --apply
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[3]
DEVOPS = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
ENG_RULES = ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
OUT = ROOT / "_oneshots"
TODAY = date.today().isoformat()
FEATURE_ID = "EPIC-008-F-012"

# Production deploy targets: exclude test modules from semantic minimum.
_TEST_RE = re.compile(r"(^|/)tests/|(^|/)test_|_test\.py$")

sys.path.insert(0, str(DEVOPS))
from builder import (  # noqa: E402
    _EXCLUDE_DIRS,
    _EXCLUDE_FILE_NAMES,
    _EXCLUDE_FILE_SUFFIXES,
    _classify_handler,
    _disposition_for,
)
import code_path_proof as cpp  # noqa: E402

# Local docker targets stage the same subtrees as their HF Space siblings.
DOCKER_HF_MIRROR: dict[str, str] = {
    "target_docker_local_findcare": "target_hf_space_findcare_backend",
    "target_docker_local_evaluatecare": "target_hf_space_evaluatecare_backend",
    "target_docker_local_sharedservices": "target_hf_space_shared_services",
}

_REPO_TOP = (
    "brain", "Code", "FindCare", "Website", "architecture", "evaluateCare",
    "sharedServices", "pipeline", "DevOps", "FrontEndApplicationLib", "Legal",
)
_REPO_PATH_RE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in _REPO_TOP) + r")/[^\s\"'\\)\]>]+"
)


@dataclass
class TargetAnalysisSpec:
    target_id: str
    entry_modules: list[str] = field(default_factory=list)
    path_roots: list[str] = field(default_factory=list)
    static_entries: list[str] = field(default_factory=list)
    ship_tree_globs: list[str] = field(default_factory=list)
    build_input_globs: list[str] = field(default_factory=list)
    extra_files: list[str] = field(default_factory=list)
    use_manifest_source_set: bool = False
    mirror_hf_target_id: str | None = None
    use_manifest_variable_paths: bool = False
    production: bool = True  # if True, drop python_test paths from semantic set


def git_tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip().replace("\\", "/") for line in out.stdout.splitlines() if line.strip()}


def content_hash(rel: str) -> str | None:
    if rel == "brain/machine_artifacts/content/deployment_architecture.json":
        return None
    p = ROOT / rel
    if not p.is_file():
        return None
    raw = p.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def file_entry(rel: str) -> dict:
    handler = _classify_handler(rel, Path(rel).name)
    entry: dict = {
        "feature_id": FEATURE_ID,
        "source_location": rel,
        "handler_type": handler,
        "disposition": _disposition_for(handler),
    }
    ch = content_hash(rel)
    if ch:
        entry["content_hash"] = ch
    return entry


def path_for_module(module: str, root: Path) -> Path | None:
    if not module or not root.is_dir():
        return None
    parts = module.split(".")
    py_file = root.joinpath(*parts).with_suffix(".py")
    if py_file.is_file():
        return py_file
    init_file = root.joinpath(*parts) / "__init__.py"
    if init_file.is_file():
        return init_file
    if len(parts) > 1:
        nested = root.joinpath(*parts[:-1]) / f"{parts[-1]}.py"
        if nested.is_file():
            return nested
    return None


def expand_globs(patterns: Iterable[str], tracked: set[str]) -> set[str]:
    out: set[str] = set()
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if "*" in pat:
            prefix = pat.split("*")[0]
            for t in tracked:
                if t.startswith(prefix):
                    if "node_modules/" in t:
                        continue
                    out.add(t)
        else:
            p = ROOT / pat
            if p.is_file():
                out.add(pat)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(ROOT).as_posix()
                        if rel in tracked:
                            out.add(rel)
    return out


def _file_excluded(path: Path) -> bool:
    if set(path.parts) & _EXCLUDE_DIRS:
        return True
    if path.name in _EXCLUDE_FILE_NAMES:
        return True
    if path.suffix.lower() in _EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def enumerate_tree(src_rel: str, tracked: set[str], production: bool) -> set[str]:
    """Tracked files under src_rel using Builder/_copy_tree exclusion rules."""
    src_rel = src_rel.replace("\\", "/").rstrip("/")
    src = ROOT / src_rel
    if not src.exists():
        return set()
    out: set[str] = set()
    if src.is_file():
        rel = src.relative_to(ROOT).as_posix()
        if rel in tracked and not _file_excluded(src):
            if not (production and _TEST_RE.search(rel)):
                out.add(rel)
        return out
    prefix = src_rel + "/"
    for rel in tracked:
        if not rel.startswith(prefix):
            continue
        path = ROOT / rel
        if not path.is_file() or _file_excluded(path):
            continue
        if production and _TEST_RE.search(rel):
            continue
        out.add(rel)
    return out


def find_target_record(manifest: dict, target_id: str) -> dict | None:
    for rec in manifest.get("DeploymentTargetRecord", []):
        if rec.get("target_id") == target_id:
            return rec
    return None


def manifest_source_set_dirs(manifest: dict, target_id: str) -> list[str]:
    rec = find_target_record(manifest, target_id)
    if not rec:
        return []
    raw = rec.get("huggingface_source_set") or []
    return [entry["src"].replace("\\", "/") for entry in raw if entry.get("src")]


def manifest_variable_paths(manifest: dict, target_id: str, tracked: set[str]) -> set[str]:
    rec = find_target_record(manifest, target_id)
    if not rec:
        return set()
    out: set[str] = set()
    for val in (rec.get("variables") or {}).values():
        if isinstance(val, str) and val.startswith("local_cert_file:"):
            rel = val.split(":", 1)[1].replace("\\", "/")
            if rel in tracked:
                out.add(rel)
    return out


def dockerfile_copy_paths(dockerfile_rel: str, tracked: set[str]) -> set[str]:
    """Expand Dockerfile COPY sources to tracked repo paths."""
    path = ROOT / dockerfile_rel
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        # COPY src1 [src2 ...] dest  — all but last token are sources
        for src in parts[1:-1]:
            src = src.replace("\\", "/")
            if src in tracked:
                out.add(src)
                continue
            out |= enumerate_tree(src, tracked, production=False)
    return out


def scan_text_paths(file_rels: Iterable[str], tracked: set[str]) -> set[str]:
    """Find repo-relative path strings embedded in file bodies."""
    out: set[str] = set()
    for rel in file_rels:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _REPO_PATH_RE.finditer(text):
            cand = m.group(0).rstrip(".,;:'\"")
            while cand and cand not in tracked:
                cand = cand.rsplit("/", 1)[0] if "/" in cand else ""
            if cand and cand in tracked:
                path = ROOT / cand
                if _file_excluded(path):
                    continue
                out.add(cand)
                if path.is_dir():
                    out |= enumerate_tree(cand, tracked, production=False)
    return out


def findcare_build_inputs() -> list[str]:
    return [
        "Code/ConversationalUX/FindCareChat/frontend/**",
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/vite.config.ts",
    ]


def workstation_ship_globs() -> list[str]:
    """DevOps-owned trees on the operator workstation (explicit, not import-derived)."""
    return [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/**",
        "architecture/EngineeringRuleEnforcement/**",
        "architecture/Conversation-logPipeline/**",
        "architecture/FindCare/**",
        "architecture/CommonFrontEndFeatures/**",
        "architecture/Testing/**",
        "Code/Shared/**",
        "pipeline/**",
        "FindCare/architectureAndDesign/**",
        "FrontEndApplicationLib/architectureAndDesign/**",
        "FrontEndApplicationLib/tests/**",
        "Legal/**",
        ".claude/**",
        ".vscode/**",
        "Code/ConversationalUX/FindCareChat/tests/**",
        "sharedServices/Code/tests/**",
        "sharedServices/APICatalogue/**",
        "enrichment_content/**",
        "code/**",
        "start_local.bat",
        "test_login_register.py",
        "test_session_cookie.py",
    ]


def workstation_meta_files(tracked: set[str]) -> list[str]:
    return sorted(
        f for f in (
            ".gitignore", ".dockerignore", "README.md", "ROADMAP.md", "CLAUDE.md",
        )
        if f in tracked
    )


class ImportWalker:
    def __init__(self, tracked: set[str]) -> None:
        self.tracked = tracked
        self.fel_lib = ROOT / "FrontEndApplicationLib" / "src" / "chathealthy_frontend_lib"

    def search_roots(self, current: Path, configured: list[Path]) -> list[Path]:
        roots: list[Path] = []
        d = current.parent
        for _ in range(8):
            if d.is_dir():
                roots.append(d)
            if d == ROOT or d.parent == d:
                break
            d = d.parent
        for r in configured:
            if r.is_dir():
                roots.append(r)
                src = r / "src"
                if src.is_dir():
                    roots.append(src)
        seen: set[str] = set()
        dedup: list[Path] = []
        for r in roots:
            key = str(r.resolve())
            if key not in seen:
                seen.add(key)
                dedup.append(r)
        return dedup

    def resolve_module(self, module: str, roots: list[Path]) -> list[str]:
        found: list[str] = []
        for root in roots:
            p = path_for_module(module, root)
            if p and p.is_file():
                rel = p.relative_to(ROOT).as_posix()
                if rel in self.tracked:
                    found.append(rel)
        return found

    def resolve_import_from(
        self, node: ast.ImportFrom, current: Path, roots: list[Path]
    ) -> list[str]:
        out: list[str] = []
        if node.level and node.level > 0:
            base = current.parent
            for _ in range(node.level - 1):
                base = base.parent
            pkg = (node.module or "").split(".") if node.module else []
            for alias in node.names:
                if alias.name == "*":
                    continue
                rel_parts = list(base.relative_to(ROOT).parts) + pkg + [f"{alias.name}.py"]
                cand = ROOT.joinpath(*rel_parts)
                if cand.is_file():
                    out.append(cand.relative_to(ROOT).as_posix())
                else:
                    init = cand.parent / alias.name / "__init__.py"
                    if init.is_file():
                        out.append(init.relative_to(ROOT).as_posix())
            return out

        mod = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                if mod:
                    out.extend(self.resolve_module(mod, roots))
                continue
            full = f"{mod}.{alias.name}" if mod else alias.name
            hits = self.resolve_module(full, roots)
            if not hits and mod:
                hits = self.resolve_module(mod, roots)
            out.extend(hits)
        return out

    def expand_package_prefix(self, prefix: str, hits: set[str]) -> None:
        """When chathealthy_frontend_lib is touched, include whole package tree."""
        if not any(h.startswith(prefix) for h in hits):
            return
        pkg_root = self.fel_lib
        if not pkg_root.is_dir():
            return
        for py in pkg_root.rglob("*.py"):
            rel = py.relative_to(ROOT).as_posix()
            if rel in self.tracked:
                hits.add(rel)

    def literal_paths(self, tree: ast.AST) -> list[str]:
        out: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.replace("\\", "/")
                if "/" in val and not val.startswith(("http://", "https://", "/tmp", "/app")):
                    for prefix in (
                        "brain/", "Code/", "FindCare/", "Website/", "architecture/",
                        "evaluateCare/", "sharedServices/", "pipeline/", "DevOps/",
                    ):
                        if val.startswith(prefix):
                            p = ROOT / val
                            if p.is_file():
                                out.append(p.relative_to(ROOT).as_posix())
        return out

    def walk_static(self, entry: str, seen: set[str]) -> None:
        queue = [entry]
        while queue:
            rel = queue.pop()
            if rel in seen or rel not in self.tracked:
                continue
            seen.add(rel)
            path = ROOT / rel
            if not path.is_file():
                continue
            if path.suffix in (".html", ".htm", ".js", ".jsx", ".tsx", ".css"):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for m in re.finditer(r'''(?:src|href)=["']([^"']+)["']''', text):
                    self._enqueue_static_ref(m.group(1), path, queue, seen)
                for m in re.finditer(r'''import\s+.*?from\s+["']([^"']+)["']''', text):
                    self._enqueue_static_ref(m.group(1), path, queue, seen)
                for m in re.finditer(r'''import\s*\(\s*["']([^"']+)["']''', text):
                    self._enqueue_static_ref(m.group(1), path, queue, seen)
            elif path.suffix == ".css":
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for m in re.finditer(r"url\(\s*['\"]?([^)'\"]+)", text):
                    self._enqueue_static_ref(m.group(1), path, queue, seen)

    def _enqueue_static_ref(self, ref: str, base: Path, queue: list[str], seen: set[str]) -> None:
        ref = ref.split("?")[0].split("#")[0]
        if not ref or ref.startswith(("http://", "https://", "//", "data:", "mailto:")):
            return
        if ref.startswith("/"):
            ref = ref.lstrip("/")
            resolved = ROOT / "Website" / ref
        else:
            resolved = (base.parent / ref).resolve()
        try:
            rel_ref = resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return
        if rel_ref not in seen and rel_ref in self.tracked:
            queue.append(rel_ref)

    def walk_python(
        self,
        entries: list[str],
        configured_roots: list[Path],
        production: bool,
    ) -> set[str]:
        seen: set[str] = set()
        queue = list(entries)
        while queue:
            rel = queue.pop()
            if rel in seen or rel not in self.tracked:
                continue
            if production and _TEST_RE.search(rel):
                continue
            seen.add(rel)
            path = ROOT / rel
            if not path.is_file() or path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            roots = self.search_roots(path, configured_roots)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for hit in self.resolve_module(alias.name, roots):
                            if hit not in seen:
                                queue.append(hit)
                elif isinstance(node, ast.ImportFrom):
                    for hit in self.resolve_import_from(node, path, roots):
                        if hit not in seen:
                            queue.append(hit)
            for hit in self.literal_paths(tree):
                if hit not in seen:
                    queue.append(hit)
        self.expand_package_prefix("FrontEndApplicationLib/src/chathealthy_frontend_lib", seen)
        if production:
            seen = {p for p in seen if not _TEST_RE.search(p)}
        return seen


def hook_entry_points() -> list[str]:
    out: list[str] = []
    if ENG_RULES.is_file():
        data = json.loads(ENG_RULES.read_text(encoding="utf-8"))
        for rule in data.get("rules", {}).get("rule", []):
            for enf in rule.get("enforcements", {}).get("enforcement", []):
                p = enf.get("executable_path", "").replace("\\", "/")
                if p:
                    out.append(p)
    settings = ROOT / ".claude" / "settings.json"
    if settings.is_file():
        out.append(".claude/settings.json")
    return sorted(set(out))


def governance_static_files(tracked: set[str]) -> set[str]:
    """DevOps-owned bytes referenced by governance, not runtime app code."""
    out: set[str] = set()
    prefixes = (
        "brain/BusinessArtifacts/",
        "brain/machine_artifacts/content/",
        "Website/schemas/",
        ".claude/",
    )
    for t in tracked:
        if any(t.startswith(p) for p in prefixes):
            out.add(t)
    if "CLAUDE.md" in tracked:
        out.add("CLAUDE.md")
    # CLAUDE.md @-references
    claude_md = ROOT / "CLAUDE.md"
    if claude_md.is_file():
        text = claude_md.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"@(\S+)", text):
            ref = m.group(1).replace("\\", "/")
            if ref in tracked:
                out.add(ref)
    return out


def website_html_entries(tracked: set[str]) -> list[str]:
    return sorted(f for f in tracked if f.startswith("Website/") and f.endswith((".html", ".htm")))


def build_target_specs(tracked: set[str]) -> dict[str, TargetAnalysisSpec]:
    html_entries = website_html_entries(tracked)
    website_extras = [
        f for f in tracked
        if f in ("Website/_headers", "Website/favicon.png") or f.startswith("Website/schemas/")
    ]
    findcare_build = findcare_build_inputs()
    ws_globs = workstation_ship_globs()
    ws_meta = workstation_meta_files(tracked)
    backend_roots = [
        "Code/ConversationalUX/FindCareChat/backend",
        "FindCare",
        "Code/Shared",
        "FrontEndApplicationLib/src",
    ]
    return {
        "target_hf_space_findcare_backend": TargetAnalysisSpec(
            target_id="target_hf_space_findcare_backend",
            entry_modules=["Code/ConversationalUX/FindCareChat/backend/app.py"],
            path_roots=backend_roots,
            extra_files=["DevOps/FindCareBackend/requirements.txt", "DevOps/FindCareBackend/Dockerfile"],
            build_input_globs=findcare_build,
            use_manifest_source_set=True,
            use_manifest_variable_paths=True,
        ),
        "target_docker_local_findcare": TargetAnalysisSpec(
            target_id="target_docker_local_findcare",
            entry_modules=["Code/ConversationalUX/FindCareChat/backend/app.py"],
            path_roots=backend_roots,
            extra_files=["DevOps/FindCareBackend/requirements.txt", "DevOps/FindCareBackend/Dockerfile"],
            build_input_globs=findcare_build,
            use_manifest_source_set=True,
            mirror_hf_target_id="target_hf_space_findcare_backend",
            use_manifest_variable_paths=True,
        ),
        "target_hf_space_evaluatecare_backend": TargetAnalysisSpec(
            target_id="target_hf_space_evaluatecare_backend",
            entry_modules=["evaluateCare/Code/app.py"],
            path_roots=["evaluateCare/Code", "FrontEndApplicationLib/src"],
            extra_files=["evaluateCare/Code/requirements.txt", "evaluateCare/Code/Dockerfile"],
            use_manifest_source_set=True,
            use_manifest_variable_paths=True,
        ),
        "target_docker_local_evaluatecare": TargetAnalysisSpec(
            target_id="target_docker_local_evaluatecare",
            entry_modules=["evaluateCare/Code/app.py"],
            path_roots=["evaluateCare/Code", "FrontEndApplicationLib/src"],
            extra_files=["evaluateCare/Code/requirements.txt", "evaluateCare/Code/Dockerfile"],
            use_manifest_source_set=True,
            mirror_hf_target_id="target_hf_space_evaluatecare_backend",
            use_manifest_variable_paths=True,
        ),
        "target_hf_space_shared_services": TargetAnalysisSpec(
            target_id="target_hf_space_shared_services",
            entry_modules=["sharedServices/Code/app.py"],
            path_roots=["sharedServices/Code", "FrontEndApplicationLib/src"],
            extra_files=["sharedServices/Code/requirements.txt", "sharedServices/Code/Dockerfile"],
            use_manifest_source_set=True,
            use_manifest_variable_paths=True,
        ),
        "target_docker_local_sharedservices": TargetAnalysisSpec(
            target_id="target_docker_local_sharedservices",
            entry_modules=["sharedServices/Code/app.py"],
            path_roots=["sharedServices/Code", "FrontEndApplicationLib/src"],
            extra_files=["sharedServices/Code/requirements.txt", "sharedServices/Code/Dockerfile"],
            use_manifest_source_set=True,
            mirror_hf_target_id="target_hf_space_shared_services",
            use_manifest_variable_paths=True,
        ),
        "target_cloudflare_pages_website": TargetAnalysisSpec(
            target_id="target_cloudflare_pages_website",
            static_entries=html_entries,
            ship_tree_globs=["Website/**"],
            extra_files=website_extras,
            production=False,
        ),
        "target_docker_local_website": TargetAnalysisSpec(
            target_id="target_docker_local_website",
            entry_modules=["architecture/DevOpsBuildDeployAndEnvironmentManagement/_start_website.py"],
            path_roots=["architecture/DevOpsBuildDeployAndEnvironmentManagement"],
            static_entries=html_entries,
            ship_tree_globs=["Website/**"],
            extra_files=[
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/WebsiteWrapper/Dockerfile",
            ],
            production=False,
        ),
        "target_host_local_workstation": TargetAnalysisSpec(
            target_id="target_host_local_workstation",
            entry_modules=[
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/build_chathealthy.py",
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py",
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/crosswalk.py",
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/record_loader.py",
                "Code/Shared/ops/tools/chathealthy_devops_boot.py",
            ]
            + hook_entry_points(),
            path_roots=[
                "architecture/DevOpsBuildDeployAndEnvironmentManagement",
                "architecture/EngineeringRuleEnforcement/code",
                "architecture/Conversation-logPipeline",
                "Code/Shared/ops/tools",
                "Code/Shared",
            ],
            ship_tree_globs=ws_globs,
            extra_files=ws_meta,
            production=False,
        ),
        "target_host_local_deploy_tool": TargetAnalysisSpec(
            target_id="target_host_local_deploy_tool",
            entry_modules=[
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/build_chathealthy.py",
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py",
            ],
            path_roots=["architecture/DevOpsBuildDeployAndEnvironmentManagement", "Code/Shared"],
            ship_tree_globs=[
                "architecture/DevOpsBuildDeployAndEnvironmentManagement/**",
                "Code/Shared/**",
            ],
            production=False,
        ),
        "target_azure_function_app_pipeline": TargetAnalysisSpec(
            target_id="target_azure_function_app_pipeline",
            entry_modules=[
                "pipeline/ChatHealthyDataPipelinesGatewayFunctionApp.py",
                "pipeline/Code/function_app.py",
            ],
            path_roots=["pipeline", "pipeline/Code"],
            ship_tree_globs=["pipeline/**"],
            extra_files=["pipeline/Code/host.json"],
            production=False,
        ),
        "target_azure_automation_runbook_reservation_reaper": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_reservation_reaper",
            entry_modules=["pipeline/Code/runbooks/reservation_reaper.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/runbooks/reservation_reaper.py"],
        ),
        "target_azure_automation_runbook_change_db_version": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_change_db_version",
            entry_modules=["pipeline/Code/change_db_version.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/change_db_version.py"],
        ),
        "target_azure_automation_runbook_chdm_orchestrator": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_chdm_orchestrator",
            entry_modules=["pipeline/Code/runbooks/data_migrator/orchestrator.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/runbooks/data_migrator/**"],
        ),
        "target_azure_automation_runbook_chdm_provisioner": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_chdm_provisioner",
            entry_modules=["pipeline/Code/runbooks/data_migrator/provisioner.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/runbooks/data_migrator/**"],
        ),
        "target_azure_automation_runbook_chdm_migrator": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_chdm_migrator",
            entry_modules=["pipeline/Code/runbooks/data_migrator/migrator.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/runbooks/data_migrator/**"],
        ),
        "target_azure_automation_runbook_chdm_deprovisioner": TargetAnalysisSpec(
            target_id="target_azure_automation_runbook_chdm_deprovisioner",
            entry_modules=["pipeline/Code/runbooks/data_migrator/deprovisioner.py"],
            path_roots=["pipeline/Code"],
            ship_tree_globs=["pipeline/Code/runbooks/data_migrator/**"],
        ),
    }


def analyze_target(
    spec: TargetAnalysisSpec,
    walker: ImportWalker,
    tracked: set[str],
    manifest: dict,
) -> set[str]:
    out: set[str] = set()

    source_target = spec.mirror_hf_target_id or spec.target_id
    if spec.use_manifest_source_set:
        for src in manifest_source_set_dirs(manifest, source_target):
            out |= enumerate_tree(src, tracked, spec.production)

    out |= expand_globs(spec.ship_tree_globs, tracked)
    out |= expand_globs(spec.build_input_globs, tracked)

    for rel in spec.extra_files:
        if rel in tracked:
            out.add(rel)
        if rel.endswith("Dockerfile"):
            out |= dockerfile_copy_paths(rel, tracked)

    if spec.use_manifest_variable_paths:
        out |= manifest_variable_paths(manifest, spec.target_id, tracked)
        if spec.mirror_hf_target_id:
            out |= manifest_variable_paths(manifest, spec.mirror_hf_target_id, tracked)

    static_hits: set[str] = set()
    for s in spec.static_entries:
        walker.walk_static(s, static_hits)
    out |= static_hits

    # Supplementary import/literal closure when ship trees are partial (runbooks, etc.).
    if spec.entry_modules and spec.path_roots and not spec.use_manifest_source_set:
        roots = [ROOT / r for r in spec.path_roots]
        out |= walker.walk_python(spec.entry_modules, roots, spec.production)

    if out:
        out |= scan_text_paths(out, tracked)

    if spec.target_id == "target_host_local_workstation":
        out |= governance_static_files(tracked)

    return {p for p in out if p in tracked}


def analyze_all(tracked: set[str], manifest: dict) -> dict[str, set[str]]:
    """Semantic file sets = code-path proof only (see code_path_proof.py)."""
    proofs = cpp.prove_all(manifest, tracked)
    return {tid: set(tp.files) for tid, tp in proofs.items()}


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def declared_files(manifest: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for rec in manifest.get("DeploymentTargetRecord", []):
        out[rec["target_id"]] = {f["source_location"] for f in rec.get("files", [])}
    return out


def apply_semantic_files(manifest: dict, semantic: dict[str, set[str]], apply_targets: set[str]) -> None:
    for rec in manifest.get("DeploymentTargetRecord", []):
        tid = rec["target_id"]
        if tid not in apply_targets:
            continue
        files = sorted(semantic.get(tid, set()))
        rec["files"] = [file_entry(rel) for rel in files]


def write_gap_report(
    tracked: set[str],
    semantic: dict[str, set[str]],
    declared: dict[str, set[str]],
    path: Path,
) -> None:
    all_targets = sorted(set(semantic) | set(declared))
    union_semantic = set()
    union_declared = set()
    for t in all_targets:
        union_semantic |= semantic.get(t, set())
        union_declared |= declared.get(t, set())

    rows: list[dict] = []
    for rel in sorted(tracked):
        sem = [t for t in all_targets if rel in semantic.get(t, set())]
        decl = [t for t in all_targets if rel in declared.get(t, set())]
        if rel in union_semantic and rel in union_declared:
            if set(sem) == set(decl) or (set(decl) <= set(sem)):
                disp = "ALIGNED"
            elif set(decl) - set(sem):
                disp = "DECLARED_BLOAT"
            else:
                disp = "SEMANTIC_GAP"
        elif rel in union_declared:
            disp = "DECLARED_ONLY"
        elif rel in union_semantic:
            disp = "SEMANTIC_ONLY"
        else:
            disp = "UNOWNED"
        rows.append({
            "file_path": rel,
            "disposition": disp,
            "semantic_targets": ";".join(sem),
            "declared_targets": ";".join(decl),
        })

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write semantic files[] into manifest for selected targets")
    args = parser.parse_args()

    tracked = git_tracked()
    manifest = load_manifest()
    declared = declared_files(manifest)
    all_proofs = cpp.prove_all(manifest, tracked)
    semantic = {tid: set(tp.files) for tid, tp in all_proofs.items()}

    proof_path = OUT / f"code_path_proof_{TODAY}.jsonl"
    summary_path = OUT / f"target_proven_files_{TODAY}.json"
    cpp.write_proof_report(proof_path, all_proofs)
    cpp.write_target_summary(summary_path, all_proofs)

    OUT.mkdir(parents=True, exist_ok=True)
    inv_path = OUT / f"semantic_inventory_{TODAY}.json"
    stats = {
        tid: {
            "semantic_count": len(semantic[tid]),
            "declared_count": len(declared.get(tid, set())),
            "only_semantic": len(semantic[tid] - declared.get(tid, set())),
            "only_declared": len(declared.get(tid, set()) - semantic[tid]),
        }
        for tid in sorted(semantic)
    }
    inv_path.write_text(
        json.dumps(
            {
                "date": TODAY,
                "semantic": {k: sorted(v) for k, v in semantic.items()},
                "stats": stats,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    gap_path = OUT / f"semantic_gap_bloat_{TODAY}.csv"
    write_gap_report(tracked, semantic, declared, gap_path)

    print(f"Target proven files:  {summary_path}")
    print(f"Code-path audit:      {proof_path}")
    print(f"\nPer-target proven ownership (code-path derived):")
    for tid in sorted(all_proofs):
        tp = all_proofs[tid]
        kinds: dict[str, int] = {}
        for e in tp.edges:
            kinds[e.path_kind] = kinds.get(e.path_kind, 0) + 1
        kind_str = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        print(f"  {tid}: {len(tp.files)} files ({kind_str})")

    if args.apply:
        apply_targets = {rec["target_id"] for rec in manifest.get("DeploymentTargetRecord", [])}
        apply_semantic_files(manifest, semantic, apply_targets)
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"\nApplied semantic files[] to all {len(apply_targets)} targets.")

        import export_deployment_deliverables as exp  # noqa: E402

        exp.main()
        print("Regenerated pure workbook + docx.")


if __name__ == "__main__":
    main()
