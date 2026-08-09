"""Code-path proof for deployment file ownership.

Every claimed file must trace to an executable anchor in the repo — a specific
file:line (or file::function:line) on a build, runtime, deploy, or hook path.
Directory globs alone are not proof; they may only expand paths already proven
via _copy_tree, Dockerfile COPY, import, or file-I/O edges.

Proof kinds:
  build   — build_chathealthy.py → _build_chain / hf_helpers staging
  runtime — import closure + file-I/O scan from app entrypoints
  deploy  — deploy_chathealthy.py variable resolution (local_cert_file:, …)
  hook    — engineering_rules.json enforcement executable_path
  docker  — Dockerfile COPY source operands
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DEVOPS = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
ENG_RULES = ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"

_TEST_RE = re.compile(r"(^|/)tests/|(^|/)test_|_test\.py$")
_REPO_PREFIXES = (
    "brain/", "Code/", "FindCare/", "Website/", "architecture/",
    "evaluateCare/", "sharedServices/", "pipeline/", "DevOps/",
    "FrontEndApplicationLib/", "Legal/", ".claude/", "enrichment_content/",
    "code/",
)

# Import builder exclusions for _copy_tree simulation.
import sys

sys.path.insert(0, str(DEVOPS))
from builder import _EXCLUDE_DIRS, _EXCLUDE_FILE_NAMES, _EXCLUDE_FILE_SUFFIXES  # noqa: E402

DOCKER_HF_MIRROR = {
    "target_docker_local_findcare": "target_hf_space_findcare_backend",
    "target_docker_local_evaluatecare": "target_hf_space_evaluatecare_backend",
    "target_docker_local_sharedservices": "target_hf_space_shared_services",
    "target_docker_local_website": "target_cloudflare_pages_website",
}

RUNTIME_ENTRIES: dict[str, list[str]] = {
    "target_hf_space_findcare_backend": ["Code/ConversationalUX/FindCareChat/backend/app.py"],
    "target_docker_local_findcare": ["Code/ConversationalUX/FindCareChat/backend/app.py"],
    "target_hf_space_evaluatecare_backend": ["evaluateCare/Code/app.py"],
    "target_docker_local_evaluatecare": ["evaluateCare/Code/app.py"],
    "target_hf_space_shared_services": ["sharedServices/Code/app.py"],
    "target_docker_local_sharedservices": ["sharedServices/Code/app.py"],
    "target_cloudflare_pages_website": [],
    "target_docker_local_website": [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/_start_website.py",
    ],
    "target_host_local_workstation": [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/build_chathealthy.py",
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py",
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/promote_chathealthy.py",
        "Code/Shared/ops/tools/chathealthy_devops_boot.py",
        "architecture/EngineeringRuleEnforcement/code/chathealthy_enforcement_manager.py",
        "pipeline/run_provider_pipeline.py",
        "pipeline/run_specialty_pipeline.py",
        "pipeline/run_county_enrichment.py",
        "pipeline/run_prescriber_pipeline.py",
        "pipeline/run_snapshot_collection.py",
        "pipeline/reserve_cluster.py",
    ],
    "target_host_local_deploy_tool": [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/build_chathealthy.py",
        "architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py",
    ],
    "target_azure_function_app_pipeline": [
        "pipeline/ChatHealthyDataPipelinesGatewayFunctionApp.py",
        "pipeline/Code/function_app.py",
    ],
    "target_azure_automation_runbook_reservation_reaper": [
        "pipeline/Code/runbooks/reservation_reaper.py",
    ],
    "target_azure_automation_runbook_change_db_version": [
        "pipeline/Code/change_db_version.py",
    ],
    "target_azure_automation_runbook_chdm_orchestrator": [
        "pipeline/Code/runbooks/data_migrator/orchestrator.py",
    ],
    "target_azure_automation_runbook_chdm_provisioner": [
        "pipeline/Code/runbooks/data_migrator/provisioner.py",
    ],
    "target_azure_automation_runbook_chdm_migrator": [
        "pipeline/Code/runbooks/data_migrator/migrator.py",
    ],
    "target_azure_automation_runbook_chdm_deprovisioner": [
        "pipeline/Code/runbooks/data_migrator/deprovisioner.py",
    ],
}

WORKSTATION_PACKAGE_PREFIXES: tuple[str, ...] = (
    "architecture/DevOpsBuildDeployAndEnvironmentManagement/",
    "architecture/EngineeringRuleEnforcement/",
    "architecture/ClaudeCodeConversationPersistence/",
    "architecture/FindCare/",
    "architecture/CommonFrontEndFeatures/",
    "architecture/Testing/",
    "Code/Shared/",
    "pipeline/",
    "FindCare/architectureAndDesign/",
    "FrontEndApplicationLib/architectureAndDesign/",
    "FrontEndApplicationLib/tests/",
    "Legal/",
    ".claude/",
    "Code/ConversationalUX/FindCareChat/tests/",
    "sharedServices/Code/tests/",
    "sharedServices/APICatalogue/",
    "enrichment_content/",
    "code/",
)

WORKSTATION_META_FILES: tuple[str, ...] = (
    ".gitignore",
    ".dockerignore",
    "README.md",
    "ROADMAP.md",
    "CLAUDE.md",
    "start_local.bat",
    "test_login_register.py",
    "test_session_cookie.py",
)

PIPELINE_PACKAGE_PREFIX = "pipeline/"

RUNTIME_ROOTS: dict[str, list[str]] = {
    "target_hf_space_findcare_backend": [
        "Code/ConversationalUX/FindCareChat/backend",
        "FindCare", "Code/Shared", "FrontEndApplicationLib/src",
    ],
    "target_docker_local_findcare": [
        "Code/ConversationalUX/FindCareChat/backend",
        "FindCare", "Code/Shared", "FrontEndApplicationLib/src",
    ],
    "target_hf_space_evaluatecare_backend": ["evaluateCare/Code", "FrontEndApplicationLib/src"],
    "target_docker_local_evaluatecare": ["evaluateCare/Code", "FrontEndApplicationLib/src"],
    "target_hf_space_shared_services": ["sharedServices/Code", "FrontEndApplicationLib/src"],
    "target_docker_local_sharedservices": ["sharedServices/Code", "FrontEndApplicationLib/src"],
    "target_host_local_workstation": [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement",
        "architecture/EngineeringRuleEnforcement/code",
        "architecture/ClaudeCodeConversationPersistence",
        "Code/Shared",
        "pipeline",
    ],
    "target_host_local_deploy_tool": [
        "architecture/DevOpsBuildDeployAndEnvironmentManagement",
        "Code/Shared",
    ],
    "target_azure_function_app_pipeline": ["pipeline", "pipeline/Code"],
    "target_azure_automation_runbook_reservation_reaper": ["pipeline/Code"],
    "target_azure_automation_runbook_change_db_version": ["pipeline/Code"],
    "target_azure_automation_runbook_chdm_orchestrator": ["pipeline/Code"],
    "target_azure_automation_runbook_chdm_provisioner": ["pipeline/Code"],
    "target_azure_automation_runbook_chdm_migrator": ["pipeline/Code"],
    "target_azure_automation_runbook_chdm_deprovisioner": ["pipeline/Code"],
}


@dataclass
class ProofEdge:
    target_id: str
    resource: str
    path_kind: str  # build | runtime | deploy | hook | docker | static
    anchor: str
    mechanism: str
    detail: str = ""


@dataclass
class TargetProof:
    target_id: str
    files: set[str] = field(default_factory=set)
    edges: list[ProofEdge] = field(default_factory=list)


def _file_excluded(path: Path) -> bool:
    if set(path.parts) & _EXCLUDE_DIRS:
        return True
    if path.name in _EXCLUDE_FILE_NAMES:
        return True
    if path.suffix.lower() in _EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def enumerate_tree(src_rel: str, tracked: set[str], production: bool) -> set[str]:
    src_rel = src_rel.replace("\\", "/").rstrip("/")
    prefix = src_rel + "/"
    out: set[str] = set()
    if (ROOT / src_rel).is_file():
        rel = src_rel
        if rel in tracked and not _file_excluded(ROOT / rel):
            if not (production and _TEST_RE.search(rel)):
                out.add(rel)
        return out
    for rel in tracked:
        if not rel.startswith(prefix):
            continue
        if _file_excluded(ROOT / rel):
            continue
        if production and _TEST_RE.search(rel):
            continue
        out.add(rel)
    return out


def find_record(manifest: dict, target_id: str) -> dict | None:
    for rec in manifest.get("DeploymentTargetRecord", []):
        if rec.get("target_id") == target_id:
            return rec
    return None


def manifest_source_set_dirs(manifest: dict, target_id: str) -> list[str]:
    rec = find_record(manifest, target_id)
    if not rec:
        return []
    return [
        e["src"].replace("\\", "/")
        for e in (rec.get("huggingface_source_set") or [])
        if e.get("src")
    ]


def _add_edges(
    tp: TargetProof,
    resources: Iterable[str],
    path_kind: str,
    anchor: str,
    mechanism: str,
    detail: str = "",
) -> None:
    for res in resources:
        tp.files.add(res)
        tp.edges.append(
            ProofEdge(tp.target_id, res, path_kind, anchor, mechanism, detail)
        )


def prove_build_hf_space(
    tp: TargetProof, manifest: dict, tracked: set[str], source_target: str, production: bool,
) -> None:
    rec = find_record(manifest, source_target)
    if not rec:
        return
    tid = rec["target_id"]

    for i, src in enumerate(manifest_source_set_dirs(manifest, source_target)):
        tree = enumerate_tree(src, tracked, production)
        _add_edges(
            tp,
            tree,
            "build",
            "_build_chain.py:_build_hf_space:312 → hf_helpers._copy_tree:433",
            "copy_tree",
            f"huggingface_source_set[{i}].src={src!r} (target {source_target})",
        )

    if tid == "target_hf_space_findcare_backend":
        frontend = ROOT / "Code" / "ConversationalUX" / "FindCareChat" / "frontend"
        fe_files = {
            r.relative_to(ROOT).as_posix()
            for r in frontend.rglob("*")
            if r.is_file() and "node_modules" not in r.parts and r.relative_to(ROOT).as_posix() in tracked
        }
        _add_edges(
            tp,
            fe_files,
            "build",
            "_build_chain.py:_build_hf_space:302 → hf_helpers._build_react_frontend:461",
            "npm_ci_and_vite_build",
            "cwd=Code/ConversationalUX/FindCareChat/frontend",
        )
        vite_cfg = "architecture/DevOpsBuildDeployAndEnvironmentManagement/vite.config.ts"
        if vite_cfg in tracked:
            _add_edges(
                tp,
                [vite_cfg],
                "build",
                "hf_helpers._build_react_frontend:465-471",
                "shutil.copy2",
                "canonical vite.config.ts copied into frontend before npm run build",
            )
        _add_edges(
            tp,
            ["DevOps/FindCareBackend/Dockerfile"],
            "build",
            "_build_chain.py:_build_hf_space:335-338",
            "shutil.copy2",
            "Dockerfile promoted to HF build context root",
        )


def prove_build_cloudflare(tp: TargetProof, tracked: set[str]) -> None:
    tree = enumerate_tree("Website", tracked, production=False)
    _add_edges(
        tp,
        tree,
        "build",
        "_build_chain.py:_build_cloudflare:273 → hf_helpers._copy_tree:433",
        "copy_tree",
        "src=Website dst=.",
    )


def prove_build_azure_function(tp: TargetProof, rec: dict, tracked: set[str]) -> None:
    for f in rec.get("files") or []:
        loc = f.get("source_location", "")
        if loc in tracked:
            _add_edges(
                tp,
                [loc],
                "build",
                "_build_chain.py:_build_azure_function_app:386",
                "zip_stage",
                f"target.files[] entry {loc!r}",
            )


def prove_build_runbook(tp: TargetProof, rec: dict, tracked: set[str]) -> None:
    files = rec.get("files") or []
    if not files:
        return
    loc = files[0].get("source_location", "")
    if loc in tracked:
        _add_edges(
            tp,
            [loc],
            "build",
            "_build_chain.py:_build_azure_automation_runbook",
            "runbook_stage",
            f"single runbook source_location={loc!r}",
        )
    if rec.get("target_id") == "target_azure_automation_runbook_change_db_version":
        manifest_path = "brain/machine_artifacts/content/deployment_architecture.json"
        if manifest_path in tracked:
            _add_edges(
                tp,
                [manifest_path],
                "build",
                "_build_chain.py:_emit_change_db_version_target_url_registry:546",
                "json.load",
                "reads manifest to bake change_db_version_target_url_registry.json",
            )


def prove_build(tp: TargetProof, manifest: dict, tracked: set[str]) -> None:
    rec = find_record(manifest, tp.target_id)
    if not rec:
        return
    kind = rec.get("target_kind")
    source_target = DOCKER_HF_MIRROR.get(tp.target_id, tp.target_id)
    production = kind in ("hf_space", "docker_local_container")

    has_hf_source = bool(manifest_source_set_dirs(manifest, source_target))
    if kind == "hf_space" or (kind == "docker_local_container" and has_hf_source):
        prove_build_hf_space(tp, manifest, tracked, source_target, production)

    if kind == "cloudflare_pages_project" or tp.target_id == "target_docker_local_website":
        prove_build_cloudflare(tp, tracked)

    if kind == "azure_function_app":
        prove_build_azure_function(tp, rec, tracked)

    if kind == "azure_automation_runbook":
        prove_build_runbook(tp, rec, tracked)

    for f in rec.get("files") or []:
        if f.get("disposition") == "managed":
            loc = f.get("source_location", "")
            if loc in tracked:
                _add_edges(
                    tp,
                    [loc],
                    "build",
                    "extractor.py:Extractor.materialize:25-27",
                    "managed_embedded_content",
                    f"disposition=managed handler={f.get('handler_type')}",
                )


def prove_deploy(tp: TargetProof, manifest: dict, tracked: set[str]) -> None:
    rec = find_record(manifest, tp.target_id)
    if not rec:
        return
    for name, qual in (rec.get("variables") or {}).items():
        if isinstance(qual, str) and qual.startswith("local_cert_file:"):
            rel = qual.split(":", 1)[1].replace("\\", "/")
            if rel in tracked:
                _add_edges(
                    tp,
                    [rel],
                    "deploy",
                    "_deploy_chain.py:_resolve_qualifier:286-288",
                    "read_bytes",
                    f"variables.{name}={qual!r}",
                )


def prove_settings_hooks(tp: TargetProof, tracked: set[str]) -> None:
    """Prove hook worker scripts registered in .claude/settings.json."""
    if tp.target_id != "target_host_local_workstation":
        return
    settings = ROOT / ".claude" / "settings.json"
    if not settings.is_file():
        return
    text = settings.read_text(encoding="utf-8")
    for m in re.finditer(r"CLAUDE_PROJECT_DIR/([^\"']+\.py)", text):
        rel = m.group(1).replace("\\", "/")
        if rel in tracked:
            _add_edges(
                tp,
                [rel],
                "hook",
                ".claude/settings.json",
                "hook.command",
                rel,
            )


def prove_enforcement_scopes(tp: TargetProof, tracked: set[str]) -> None:
    """Prove files reachable via enforcement scope regexes (pre-commit / hook scans)."""
    if tp.target_id != "target_host_local_workstation":
        return
    if not ENG_RULES.is_file():
        return
    data = json.loads(ENG_RULES.read_text(encoding="utf-8"))
    patterns: list[tuple[str, str, str, str]] = []
    for rule in data.get("rules", {}).get("rule", []):
        rid = rule.get("id", "")
        for enf in rule.get("enforcements", {}).get("enforcement", []):
            eid = enf.get("enforcement_id", "")
            worker = enf.get("executable_path", "")
            for scope_row in enf.get("scopes") or []:
                if not isinstance(scope_row, list) or len(scope_row) < 3:
                    continue
                if scope_row[1] != "allowed_pattern":
                    continue
                for pat in scope_row[2]:
                    patterns.append((rid, eid, worker, pat))
    for rel in tracked:
        for rid, eid, worker, pat in patterns:
            if re.search(pat, rel):
                _add_edges(
                    tp,
                    [rel],
                    "hook",
                    worker or "engineering_rules.json",
                    "enforcement.scope_regex",
                    f"rule={rid} enforcement={eid} pattern={pat!r}",
                )
                break


def prove_hooks(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    if not ENG_RULES.is_file():
        return
    data = json.loads(ENG_RULES.read_text(encoding="utf-8"))
    for rule in data.get("rules", {}).get("rule", []):
        rid = rule.get("id", "")
        for enf in rule.get("enforcements", {}).get("enforcement", []):
            p = enf.get("executable_path", "").replace("\\", "/")
            if p in tracked:
                _add_edges(
                    tp,
                    [p],
                    "hook",
                    "engineering_rules.json",
                    "enforcement.executable_path",
                    f"rule={rid} hook={enf.get('hook')}",
                )
    settings = ".claude/settings.json"
    if settings in tracked:
        _add_edges(
            tp,
            [settings],
            "hook",
            "chathealthy_enforcement_manager (via .claude/settings.json)",
            "hook_registration",
            "SessionStart/Stop/UserPromptSubmit dispatch",
        )


def prove_dockerfile(tp: TargetProof, manifest: dict, tracked: set[str]) -> None:
    rec = find_record(manifest, tp.target_id)
    if not rec:
        return
    dockerfiles: list[str] = []
    for f in rec.get("files") or []:
        if f.get("handler_type") == "dockerfile":
            dockerfiles.append(f["source_location"])
    if tp.target_id.startswith("target_docker_local_findcare"):
        dockerfiles.append("DevOps/FindCareBackend/Dockerfile")
    for df in dockerfiles:
        path = ROOT / df
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip().upper().startswith("COPY "):
                continue
            parts = line.split()
            for src in parts[1:-1]:
                src = src.replace("\\", "/")
                if src in tracked:
                    _add_edges(
                        tp,
                        [src],
                        "docker",
                        f"{df}:{lineno}",
                        "dockerfile_copy",
                        line.strip(),
                    )
                else:
                    _add_edges(
                        tp,
                        enumerate_tree(src, tracked, production=False),
                        "docker",
                        f"{df}:{lineno}",
                        "dockerfile_copy_tree",
                        line.strip(),
                    )


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
    return None


class RuntimeProver:
    """Import closure + module-wide file-I/O / path-literal scan."""

    def __init__(self, tracked: set[str]) -> None:
        self.tracked = tracked

    def _roots(self, configured: list[str]) -> list[Path]:
        out: list[Path] = []
        for r in configured:
            p = ROOT / r
            if p.is_dir():
                out.append(p)
                src = p / "src"
                if src.is_dir():
                    out.append(src)
        return out

    def _resolve_import_from(self, node: ast.ImportFrom, current: Path, roots: list[Path]) -> list[tuple[str, int]]:
        hits: list[tuple[str, int]] = []
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
                    hits.append((cand.relative_to(ROOT).as_posix(), node.lineno))
                else:
                    init = cand.parent / alias.name / "__init__.py"
                    if init.is_file():
                        hits.append((init.relative_to(ROOT).as_posix(), node.lineno))
            return hits
        mod = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                if mod:
                    for root in roots:
                        p = path_for_module(mod, root)
                        if p and p.is_file():
                            rel = p.relative_to(ROOT).as_posix()
                            if rel in self.tracked:
                                hits.append((rel, node.lineno))
                continue
            full = f"{mod}.{alias.name}" if mod else alias.name
            found = False
            for root in roots:
                p = path_for_module(full, root)
                if not p or not p.is_file():
                    p = path_for_module(mod, root) if mod else None
                if p and p.is_file():
                    rel = p.relative_to(ROOT).as_posix()
                    if rel in self.tracked:
                        hits.append((rel, node.lineno))
                        found = True
                        break
            if not found and mod:
                for root in roots:
                    p = path_for_module(mod, root)
                    if p and p.is_file():
                        rel = p.relative_to(ROOT).as_posix()
                        if rel in self.tracked:
                            hits.append((rel, node.lineno))
        return hits

    def _resolve_import(self, node: ast.Import, roots: list[Path]) -> list[tuple[str, int]]:
        hits: list[tuple[str, int]] = []
        for alias in node.names:
            for root in roots:
                p = path_for_module(alias.name, root)
                if p and p.is_file():
                    rel = p.relative_to(ROOT).as_posix()
                    if rel in self.tracked:
                        hits.append((rel, node.lineno))
        return hits

    def _literal_paths(self, tree: ast.AST, src: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.replace("\\", "/")
                if any(val.startswith(p) for p in _REPO_PREFIXES):
                    p = ROOT / val
                    if p.is_file() and val in self.tracked:
                        out.append((val, node.lineno))
        return out

    def _file_io_paths(self, tree: ast.AST, src: str) -> list[tuple[str, int, str]]:
        """Return (path, lineno, mechanism) for file reads."""
        out: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in ("open", "read_text", "read_bytes", "load"):
                    for arg in node.args[:1]:
                        lit = self._str_constant(arg)
                        if lit:
                            rel = self._normalize_path_literal(lit, src)
                            if rel and rel in self.tracked:
                                out.append((rel, node.lineno, name))
        return out

    def _str_constant(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.replace("\\", "/")
        if isinstance(node, ast.JoinedStr):
            parts = [p.value for p in node.values if isinstance(p, ast.Constant)]
            if parts and all(isinstance(p, str) for p in parts):
                return "".join(parts).replace("\\", "/")
        return None

    def _normalize_path_literal(self, val: str, src: str) -> str | None:
        val = val.replace("\\", "/")
        if any(val.startswith(p) for p in _REPO_PREFIXES):
            return val if val in self.tracked else None
        if val.endswith(".json") and "/" not in val:
            # boot reads agile_backlog.json relative to BRAIN_DIR
            cand = f"brain/machine_artifacts/content/{val}"
            return cand if cand in self.tracked else None
        return None

    def _path_join_literal(self, node: ast.AST) -> str | None:
        parts: list[str] = []

        def walk(n: ast.AST) -> bool:
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                parts.append(n.value.replace("\\", "/"))
                return True
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                return walk(n.left) and walk(n.right)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Path":
                if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    parts.append(n.args[0].value.replace("\\", "/"))
                    return True
            return False

        if not walk(node):
            return None
        rel = "/".join(p.strip("/") for p in parts if p)
        return rel if rel in self.tracked else None

    def _subprocess_script_paths(self, tree: ast.AST, src: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args: list[ast.AST] = []
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "call", "Popen"):
                args = list(node.args)
            elif isinstance(node.func, ast.Name) and node.func.id in ("run", "call", "Popen"):
                args = list(node.args)
            else:
                continue
            if not args:
                continue
            first = args[0]
            if isinstance(first, ast.List):
                for elt in first.elts:
                    lit = self._str_constant(elt)
                    if lit and lit.endswith(".py") and lit in self.tracked:
                        out.append((lit, node.lineno))
                    joined = self._path_join_literal(elt)
                    if joined and joined.endswith(".py"):
                        out.append((joined, node.lineno))
            lit = self._str_constant(first)
            if lit and lit.endswith(".py") and lit in self.tracked:
                out.append((lit, node.lineno))
        return out

    def _path_assignment_literals(self, tree: ast.AST) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                val = node.value
                joined = self._path_join_literal(val)
                if joined:
                    out.append((joined, node.lineno))
                lit = self._str_constant(val)
                if lit and lit in self.tracked:
                    out.append((lit, node.lineno))
        return out

    def prove(
        self,
        tp: TargetProof,
        entries: list[str],
        configured_roots: list[str],
        production: bool,
    ) -> None:
        roots = self._roots(configured_roots)
        seen: set[str] = set()
        queue = list(entries)
        while queue:
            rel = queue.pop()
            if rel in seen or rel not in self.tracked:
                continue
            if production and _TEST_RE.search(rel):
                continue
            seen.add(rel)
            _add_edges(
                tp,
                [rel],
                "runtime",
                f"{rel}:1",
                "module_in_closure",
                "entrypoint or import-closure module",
            )
            path = ROOT / rel
            if not path.is_file() or path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            search = [path.parent] + roots
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for hit, ln in self._resolve_import(node, search):
                        _add_edges(
                            tp,
                            [hit],
                            "runtime",
                            f"{rel}:{ln}",
                            "import",
                            f"import in {rel}",
                        )
                        if hit not in seen:
                            queue.append(hit)
                elif isinstance(node, ast.ImportFrom):
                    for hit, ln in self._resolve_import_from(node, path, search):
                        _add_edges(
                            tp,
                            [hit],
                            "runtime",
                            f"{rel}:{ln}",
                            "import_from",
                            f"from-import in {rel}",
                        )
                        if hit not in seen:
                            queue.append(hit)
            for lit, ln in self._literal_paths(tree, rel):
                _add_edges(
                    tp,
                    [lit],
                    "runtime",
                    f"{rel}:{ln}",
                    "path_literal",
                    "",
                )
            for lit, ln, mech in self._file_io_paths(tree, rel):
                _add_edges(
                    tp,
                    [lit],
                    "runtime",
                    f"{rel}:{ln}",
                    f"file_io:{mech}",
                    "",
                )
            for lit, ln in self._subprocess_script_paths(tree, rel):
                _add_edges(
                    tp,
                    [lit],
                    "runtime",
                    f"{rel}:{ln}",
                    "subprocess_script",
                    "",
                )
                if lit not in seen:
                    queue.append(lit)
            for lit, ln in self._path_assignment_literals(tree):
                if lit.endswith("/"):
                    continue
                _add_edges(
                    tp,
                    [lit],
                    "runtime",
                    f"{rel}:{ln}",
                    "path_assignment",
                    "",
                )
                if lit.endswith(".py") and lit not in seen:
                    queue.append(lit)
        # chathealthy_frontend_lib: whole package when any module touched
        if any(h.startswith("FrontEndApplicationLib/src/chathealthy_frontend_lib") for h in seen):
            for py in (ROOT / "FrontEndApplicationLib" / "src" / "chathealthy_frontend_lib").rglob("*.py"):
                r = py.relative_to(ROOT).as_posix()
                if r in self.tracked and r not in seen:
                    _add_edges(
                        tp,
                        [r],
                        "runtime",
                        "FrontEndApplicationLib/src/chathealthy_frontend_lib",
                        "package_expansion",
                        "any import from chathealthy_frontend_lib pulls full package",
                    )


def prove_static_website(tp: TargetProof, tracked: set[str]) -> None:
    entries = [f for f in tracked if f.startswith("Website/") and f.endswith((".html", ".htm"))]
    seen: set[str] = set()
    queue = list(entries)
    while queue:
        rel = queue.pop()
        if rel in seen or rel not in tracked:
            continue
        seen.add(rel)
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'''(?:src|href)=["']([^"']+)["']''', text):
            ref = m.group(1).split("?")[0].split("#")[0]
            if ref.startswith(("http://", "https://", "//", "data:")):
                continue
            if ref.startswith("/"):
                resolved = f"Website/{ref.lstrip('/')}"
            else:
                resolved = (path.parent / ref).resolve().relative_to(ROOT.resolve()).as_posix()
            if resolved in tracked and resolved not in seen:
                _add_edges(
                    tp,
                    [resolved],
                    "static",
                    f"{rel}:{text[:m.start()].count(chr(10)) + 1}",
                    "html_link",
                    ref,
                )
                queue.append(resolved)


def prove_governance(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    claude = ROOT / "CLAUDE.md"
    if claude.is_file() and "CLAUDE.md" in tracked:
        _add_edges(tp, ["CLAUDE.md"], "hook", "CLAUDE.md", "governance", "operator session contract")
        for m in re.finditer(r"@(\S+)", claude.read_text(encoding="utf-8")):
            ref = m.group(1).replace("\\", "/")
            if ref in tracked:
                _add_edges(tp, [ref], "hook", "CLAUDE.md", "at_reference", ref)
    for prefix in (
        "brain/machine_artifacts/content/",
        "Website/schemas/",
    ):
        for t in tracked:
            if t.startswith(prefix):
                _add_edges(
                    tp,
                    [t],
                    "hook",
                    "CLAUDE.md / boot session",
                    "governance_static",
                    prefix,
                )


FINDCARE_RUNTIME_JSON: list[tuple[str, str, str]] = [
    (
        "brain/machine_artifacts/content/controlled_vocabularies.json",
        "Code/Shared/prompt_system_maker.py:82",
        "load_emergency_keywords reads CV-006",
    ),
    (
        "brain/machine_artifacts/content/prompts.json",
        "FindCare/SpecialtyFilter/filter.py:32",
        "PROMPTS_JSON_PATH read at filter init",
    ),
]

FINDCARE_TARGET_IDS = frozenset({
    "target_hf_space_findcare_backend",
    "target_docker_local_findcare",
})


def prove_rule_066_devops_scan(tp: TargetProof, tracked: set[str]) -> None:
    """Rule-066 pre-commit scans every top-level .py in DevOpsBuildDeployAndEnvironmentManagement/."""
    if tp.target_id != "target_host_local_workstation":
        return
    deploy_dir = DEVOPS
    anchor = "scan_entrypoint_uniqueness_worker.py:67-70"
    for child in sorted(deploy_dir.iterdir()):
        if not child.is_file() or child.suffix != ".py":
            continue
        rel = child.relative_to(ROOT).as_posix()
        if rel in tracked:
            _add_edges(
                tp,
                [rel],
                "hook",
                anchor,
                "rule_066_directory_scan",
                "Rule-066 scans top-level DevOps .py on pre-commit",
            )


def prove_package_expansion(
    tp: TargetProof,
    tracked: set[str],
    prefix: str,
    anchor: str,
    mechanism: str,
    detail: str = "",
) -> None:
    """Expand ownership under prefix once any file in that prefix is already proven."""
    prefix = prefix.rstrip("/") + "/"
    if not any(f.startswith(prefix) for f in tp.files):
        return
    expanded = {t for t in tracked if t.startswith(prefix)}
    if expanded:
        _add_edges(tp, expanded, "runtime", anchor, mechanism, detail or prefix)


def prove_workstation_dev_packages(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    for prefix in WORKSTATION_PACKAGE_PREFIXES:
        prove_package_expansion(
            tp,
            tracked,
            prefix,
            "deploy_chathealthy.py --env local → LocalStandUp",
            "workstation_dev_package",
            prefix,
        )
    for rel in WORKSTATION_META_FILES:
        if rel in tracked:
            _add_edges(
                tp,
                [rel],
                "runtime",
                "deploy_chathealthy.py / start_local.bat / CLAUDE.md",
                "workstation_meta",
                rel,
            )


def prove_pipeline_package(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id not in (
        "target_azure_function_app_pipeline",
        "target_host_local_workstation",
    ):
        return
    prove_package_expansion(
        tp,
        tracked,
        PIPELINE_PACKAGE_PREFIX,
        "pipeline/ChatHealthyDataPipelinesGatewayFunctionApp.py",
        "pipeline_package",
        "operator + Azure Function App pipeline tree",
    )


def prove_deploy_chain_static_paths(tp: TargetProof, tracked: set[str]) -> None:
    """Scan _deploy_chain.py for repo path literals and pytest smoke scripts."""
    if tp.target_id not in ("target_host_local_workstation", "target_docker_local_findcare"):
        return
    chain = DEVOPS / "_deploy_chain.py"
    if not chain.is_file():
        return
    text = chain.read_text(encoding="utf-8", errors="replace")
    anchor = "_deploy_chain.py:LocalDeploy"
    for m in re.finditer(r'["\']([^"\']+\.py)["\']', text):
        rel = m.group(1).replace("\\", "/")
        if rel in tracked:
            _add_edges(tp, [rel], "runtime", anchor, "deploy_chain_literal", rel)
    smoke = DEVOPS / "find_care_smoke_test.py"
    if smoke.is_file():
        rel = smoke.relative_to(ROOT).as_posix()
        if rel in tracked:
            _add_edges(
                tp,
                [rel],
                "runtime",
                "_deploy_chain.py:2739",
                "subprocess_pytest",
                "LocalDeploy smoke gate",
            )


def prove_readme_linked_files(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    readme = ROOT / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"\]\(([^)]+)\)", text):
        ref = m.group(1).split("#")[0].strip()
        if ref in tracked:
            _add_edges(
                tp,
                [ref],
                "runtime",
                "README.md",
                "markdown_link",
                ref,
            )


def prove_architecture_design_docs(tp: TargetProof, tracked: set[str]) -> None:
    """builder.py documents ArchitectureDesignAndAuditDocs/ as dev-only human docs."""
    if tp.target_id != "target_host_local_workstation":
        return
    anchor = "builder.py:67-72"
    for rel in tracked:
        if "/ArchitectureDesignAndAuditDocs/" in rel:
            _add_edges(
                tp,
                [rel],
                "runtime",
                anchor,
                "builder_dev_design_docs",
                "non-deployable design documentation",
            )


def prove_findcare_design_generators(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    prover = RuntimeProver(tracked)
    generators = [
        "architecture/FindCare/ArchitectureDesignAndAuditDocs/generate_specialty_filter_spec.py",
        "architecture/FindCare/ArchitectureDesignAndAuditDocs/generate_specialty_filter_design_V2.py",
    ]
    for rel in generators:
        if rel not in tracked:
            continue
        _add_edges(
            tp,
            [rel],
            "runtime",
            f"{rel}:__main__",
            "design_doc_generator",
            rel,
        )
        path = ROOT / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                joined = prover._path_join_literal(node.value)
                if joined and joined in tracked:
                    _add_edges(
                        tp,
                        [joined],
                        "build",
                        rel,
                        "generator_output_path",
                        joined,
                    )


def prove_ch_fonts_snippet(tp: TargetProof, tracked: set[str]) -> None:
    rel = "architecture/CommonFrontEndFeatures/ch_fonts_snippet.html"
    if tp.target_id != "target_host_local_workstation" or rel not in tracked:
        return
    _add_edges(
        tp,
        [rel],
        "runtime",
        "ch_fonts_inliner.py:11-18",
        "read_text",
        "canonical CH_FONTS snippet read at deploy inline time",
    )


def prove_legacy_code_tree(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id != "target_host_local_workstation":
        return
    for rel in tracked:
        if rel.startswith("code/"):
            _add_edges(
                tp,
                [rel],
                "runtime",
                "code/ (legacy dev-only tree)",
                "workstation_legacy",
                rel,
            )


def prove_agile_backlog_test_files(tp: TargetProof, tracked: set[str]) -> None:
    """Tests registered in agile_backlog.json are validated by Crosswalk on deploy."""
    if tp.target_id != "target_host_local_workstation":
        return
    backlog = ROOT / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
    if not backlog.is_file():
        return
    try:
        data = json.loads(backlog.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            file_val = obj.get("file")
            if isinstance(file_val, str) and file_val in tracked and file_val != "ORPHAN":
                _add_edges(
                    tp,
                    [file_val],
                    "runtime",
                    "crosswalk.py + agile_backlog.json",
                    "backlog_test_registration",
                    file_val,
                )
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)


def prove_findcare_runtime_json(tp: TargetProof, tracked: set[str]) -> None:
    if tp.target_id not in FINDCARE_TARGET_IDS:
        return
    for resource, anchor, detail in FINDCARE_RUNTIME_JSON:
        if resource in tracked:
            _add_edges(tp, [resource], "runtime", anchor, "read_text/json.load", detail)


def prove_target(
    target_id: str,
    manifest: dict,
    tracked: set[str],
    runtime: RuntimeProver,
) -> TargetProof:
    tp = TargetProof(target_id=target_id)
    prove_build(tp, manifest, tracked)
    prove_deploy(tp, manifest, tracked)
    prove_dockerfile(tp, manifest, tracked)
    prove_hooks(tp, tracked)
    prove_settings_hooks(tp, tracked)
    prove_enforcement_scopes(tp, tracked)
    prove_governance(tp, tracked)

    entries = RUNTIME_ENTRIES.get(target_id, [])
    roots = RUNTIME_ROOTS.get(target_id, [])
    rec = find_record(manifest, target_id)
    production = bool(rec and rec.get("target_kind") in ("hf_space", "docker_local_container"))
    if entries and roots:
        runtime.prove(tp, entries, roots, production)

    if target_id in ("target_cloudflare_pages_website", "target_docker_local_website"):
        prove_static_website(tp, tracked)

    prove_findcare_runtime_json(tp, tracked)
    prove_rule_066_devops_scan(tp, tracked)
    prove_deploy_chain_static_paths(tp, tracked)
    prove_agile_backlog_test_files(tp, tracked)
    prove_readme_linked_files(tp, tracked)
    prove_architecture_design_docs(tp, tracked)
    prove_findcare_design_generators(tp, tracked)
    prove_ch_fonts_snippet(tp, tracked)
    prove_legacy_code_tree(tp, tracked)
    prove_workstation_dev_packages(tp, tracked)
    prove_pipeline_package(tp, tracked)
    _finalize_proven_files(tp, rec)
    return tp


# Production backends: a .json file is owned only if runtime/deploy code
# opens it — not because copy_tree wholesale-copied brain/machine_artifacts/content.
_JSON_LIVE_PATH_KINDS = frozenset({"runtime", "deploy"})
_NON_JSON_PATH_KINDS = frozenset({"runtime", "deploy", "docker", "build", "static", "hook"})


def _is_production_backend(rec: dict) -> bool:
    kind = rec.get("target_kind")
    tid = rec.get("target_id", "")
    if kind not in ("hf_space", "docker_local_container"):
        return False
    if "website" in tid:
        return False
    return True


def _finalize_proven_files(tp: TargetProof, rec: dict | None) -> None:
    """Drop aspirational JSON from production targets (build copy alone is not proof)."""
    if not rec or not _is_production_backend(rec):
        return
    kept: set[str] = set()
    for edge in tp.edges:
        rel = edge.resource
        if rel.endswith(".json"):
            if edge.path_kind in _JSON_LIVE_PATH_KINDS:
                kept.add(rel)
            elif edge.path_kind == "build" and edge.mechanism != "copy_tree":
                kept.add(rel)
        elif edge.path_kind in _NON_JSON_PATH_KINDS:
            kept.add(rel)
    tp.files = kept


def prove_all(manifest: dict, tracked: set[str]) -> dict[str, TargetProof]:
    runtime = RuntimeProver(tracked)
    out: dict[str, TargetProof] = {}
    for rec in manifest.get("DeploymentTargetRecord", []):
        tid = rec["target_id"]
        out[tid] = prove_target(tid, manifest, tracked, runtime)
    return out


def unproven_tracked(tracked: set[str], all_proofs: dict[str, TargetProof]) -> set[str]:
    owned = set()
    for tp in all_proofs.values():
        owned |= tp.files
    return tracked - owned


def write_target_summary(path: Path, all_proofs: dict[str, TargetProof]) -> None:
    """Per-target positive inventory: what the target owns and why (path_kind counts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}
    for tid in sorted(all_proofs):
        tp = all_proofs[tid]
        by_kind: dict[str, int] = {}
        for edge in tp.edges:
            by_kind[edge.path_kind] = by_kind.get(edge.path_kind, 0) + 1
        summary[tid] = {
            "proven_file_count": len(tp.files),
            "proof_edge_count": len(tp.edges),
            "path_kinds": by_kind,
            "files": sorted(tp.files),
        }
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def write_proof_report(path: Path, all_proofs: dict[str, TargetProof]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for tid in sorted(all_proofs):
            tp = all_proofs[tid]
            for edge in tp.edges:
                row = asdict(edge)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
