"""scan_pydantic_ai_compliance_worker.py -- Rule-065-ENF-011.

A deterministic syntax check (AST, no model), global, exempting nothing. Four
rules for every tool/agent in ChatHealthy.ai:

  1. It uses pydantic-ai: it reaches a model only through a pydantic_ai.Agent,
     never a raw vendor SDK (openai / anthropic / google.generativeai /
     google.genai).
  2. Its input is a pydantic object: the Agent declares deps_type=<a pydantic
     BaseModel>.
  3. Its output is a pydantic object: the Agent declares output_type=<a pydantic
     BaseModel, or an output function annotated to return one>.
  4. It uses no facade: no chathealthy_lib.llm run_llm/run_llm_sync -- the
     pydantic-ai interface is used directly (agent.run / run_sync).

A tool is a file that constructs a pydantic_ai.Agent. A file that reaches a model
through a raw vendor SDK is a tool that fails rule 1. A file with neither is not
a tool and passes. No LLM is used to judge this; it is read from the syntax.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_CH_LOG = ChatHealthyLoggingService()
_RULE_ID = "Rule-065"

_FACADE_MODULE = "chathealthy_lib.llm"
_FACADE_NAMES = frozenset({"run_llm", "run_llm_sync"})
# The raw vendor SDK roots. Reaching a model through any of these is reaching it
# outside the pydantic-ai framework (rule 1).
_VENDOR_ROOTS = frozenset({"openai", "anthropic", "google.generativeai", "google.genai"})


def _vendor_root_hit(name: str) -> str | None:
    for root in _VENDOR_ROOTS:
        if name == root or name.startswith(root + "."):
            return root
    return None


def _base_names(node: ast.ClassDef) -> list[str]:
    out = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def _basemodel_classes(tree: ast.Module) -> set[str]:
    """Names of classes in this file that subclass BaseModel."""
    return {n.name for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and "BaseModel" in _base_names(n)}


def _basemodel_returning_funcs(tree: ast.Module, bm: set[str]) -> set[str]:
    """Names of functions whose return annotation is one of the BaseModels."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            r = n.returns
            if isinstance(r, ast.Name) and r.id in bm:
                out.add(n.name)
            elif isinstance(r, ast.Attribute) and r.attr in bm:
                out.add(n.name)
    return out


def _is_agent_call(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Name) and f.id == "Agent") or \
           (isinstance(f, ast.Attribute) and f.attr == "Agent")


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _ref_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def scan(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (lineno, message) for every rule broken in this module."""
    hits: list[tuple[int, str]] = []
    bm = _basemodel_classes(tree)
    out_funcs = _basemodel_returning_funcs(tree, bm)

    for node in ast.walk(tree):
        # rule 4 -- facade imported
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE_MODULE:
            for a in node.names:
                if a.name in _FACADE_NAMES:
                    hits.append((node.lineno,
                        f"rule 4: uses the facade ({a.name} from {_FACADE_MODULE}); "
                        f"expose the pydantic-ai interface directly (agent.run/run_sync)"))
        # rule 1 -- raw vendor SDK imported
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == _FACADE_MODULE:
                    hits.append((node.lineno,
                        f"rule 4: imports the facade module {_FACADE_MODULE}; use pydantic-ai directly"))
                if _vendor_root_hit(a.name):
                    hits.append((node.lineno,
                        f"rule 1: imports the raw vendor SDK {a.name}; a tool reaches a "
                        f"model only through a pydantic_ai.Agent"))
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _vendor_root_hit(mod):
                hits.append((node.lineno,
                    f"rule 1: imports from the raw vendor SDK {mod}; a tool reaches a "
                    f"model only through a pydantic_ai.Agent"))
            elif mod == "google" and any(a.name == "genai" for a in node.names):
                hits.append((node.lineno,
                    "rule 1: imports google genai directly; a tool reaches a model "
                    "only through a pydantic_ai.Agent"))
        # rule 4 -- facade defined
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _FACADE_NAMES:
            hits.append((node.lineno,
                f"rule 4: defines the facade function {node.name}; no facade over "
                f"pydantic-ai is sanctioned"))
        if isinstance(node, ast.Call):
            # rule 4 -- facade called
            called = _ref_name(node.func)
            if called in _FACADE_NAMES:
                hits.append((node.lineno,
                    f"rule 4: calls the facade function {called}; use the pydantic-ai "
                    f"interface directly (agent.run/run_sync)"))
            # rules 2 & 3 -- an Agent's input and output are pydantic objects
            if _is_agent_call(node):
                dt = _kwarg(node, "deps_type")
                ot = _kwarg(node, "output_type")
                if dt is None or _ref_name(dt) not in bm:
                    hits.append((node.lineno,
                        "rule 2: pydantic_ai.Agent has no pydantic input; declare "
                        "deps_type=<a pydantic BaseModel defined in this file>"))
                ot_name = _ref_name(ot)
                if ot is None or (ot_name not in bm and ot_name not in out_funcs):
                    hits.append((node.lineno,
                        "rule 3: pydantic_ai.Agent has no pydantic output; declare "
                        "output_type=<a pydantic BaseModel, or an output function "
                        "returning one>"))
    return hits


class ScanPydanticAiComplianceWorker(EnforcementWorker):
    """Rule-065-ENF-011: every tool/agent is a pydantic-ai agent (syntax check)."""

    SCOPE_DEFAULTS: dict[str, bool] = {"_scan_pydantic_ai_compliance": False}
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def run(self) -> int:
        any_violations = False
        for file_path in self.files:
            if not self.is_in_scope(file_path, "_scan_pydantic_ai_compliance"):
                continue
            self.files_scanned += 1
            for v in self._scan_pydantic_ai_compliance(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        _CH_LOG.info("[ENF-011] examined %d file(s), %d violation(s)",
                     self.files_scanned, self.violation_count)
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_pydantic_ai_compliance(self, file_path: str) -> list[ViolationRecord]:
        absolute = (PROJECT_ROOT / file_path).resolve()
        if not absolute.is_file():
            return []
        try:
            tree = self.parse_python(file_path, self.read_text(file_path))
        except ChatHealthyException as exc:
            return [self.uncertifiable_violation(file_path, exc, rule_id=_RULE_ID)]
        return [ViolationRecord(
                    enforcement_id=self.enforcement_id, rule_id=_RULE_ID,
                    resource=f"{file_path}:{lineno}", message=message)
                for lineno, message in scan(tree)]


def main() -> int:
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-011"
    return ScanPydanticAiComplianceWorker(enforcement_id).run()


if __name__ == "__main__":
    sys.exit(main())
