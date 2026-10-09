"""scan_llm_facade_worker.py -- Rule-065-ENF-011 worker.

Enforces the declared architecture, stipulated as of 2026-10-08: every LLM call
is made within the pydantic-ai framework (through the ChatHealthy LLM facade,
chathealthy_lib.llm), and no agent exists outside that framework. The
enforceable form: outside the facade, ChatHealthy.ai code MUST NOT reach a model
vendor directly -- not by importing a vendor SDK and not by naming a vendor's
API host. The only way left to call a model is a pydantic-ai Agent run through
the facade, so every agent is a pydantic-ai agent and every model call is
centralised.

What is flagged (AST-based, no regex per Rule-008 statement 4):
  (a) importing a model-vendor SDK: `import openai` / `from openai import ...`
      and the same for anthropic, google.generativeai, google.genai, and
      `from google import genai`.
  (b) a string literal naming a vendor's API host (generativelanguage.
      googleapis.com, api.openai.com, api.anthropic.com) -- a raw HTTP call to
      the model, bypassing the SDK and the facade alike.

`import pydantic_ai` is NOT flagged: that IS the framework. The facade
(chathealthy_lib/llm.py) is the one place a vendor SDK is legitimate (it wraps
pydantic-ai and owns the embeddings client) and is excluded by scope.
"""
from __future__ import annotations

import ast
import sys
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

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


# Vendor SDK top-level module names. An import of any of these, or of a
# submodule under them, is a direct reach for a model vendor.
FORBIDDEN_IMPORT_ROOTS = frozenset({
    "openai", "anthropic", "google.generativeai", "google.genai",
})
# A model vendor's API host appearing in a string literal is a raw HTTP call
# to the model, outside the SDK and the facade both.
VENDOR_HOSTS = (
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
)


def _import_root_hit(name: str) -> str | None:
    """Return the forbidden root when `name` is one of the vendor roots or a
    submodule of one, else None."""
    for root in FORBIDDEN_IMPORT_ROOTS:
        if name == root or name.startswith(root + "."):
            return root
    return None


def import_violations(tree: ast.Module) -> list[tuple[int, str]]:
    """`import <vendor>` / `import <vendor>.sub` / `from <vendor> import ...`
    and the `from google import genai` form."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _import_root_hit(alias.name)
                if root is not None:
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = _import_root_hit(module)
            if root is not None:
                names = ", ".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {module} import {names}"))
            elif module == "google":
                for alias in node.names:
                    if alias.name == "genai":
                        hits.append((node.lineno, "from google import genai"))
    return hits


def host_literal_violations(tree: ast.Module) -> list[tuple[int, str]]:
    """A string literal naming a model vendor's API host."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for host in VENDOR_HOSTS:
                if host in node.value:
                    hits.append((node.lineno, f"vendor API host {host!r} in a string literal"))
                    break
    return hits


def all_violations(tree: ast.Module) -> list[tuple[int, str]]:
    return import_violations(tree) + host_literal_violations(tree)


class ScanLlmFacadeEnforcementWorker(EnforcementWorker):
    """Rule-065-ENF-011: every LLM call within the pydantic-ai framework."""

    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_llm_facade": False,
    }
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def _staged_files(self) -> list[str]:
        return self.files

    def run(self) -> int:
        any_violations = False
        for file_path in self._staged_files():
            self.files_scanned += 1
            if not self.is_in_scope(file_path, "_scan_llm_facade"):
                continue
            for v in self._scan_llm_facade(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_llm_facade(self, file_path: str) -> list[ViolationRecord]:
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            staged_text = self.read_text(file_path)
            tree = self.parse_python(file_path, staged_text)
        except ChatHealthyException as exc:
            return [self.uncertifiable_violation(file_path, exc, rule_id="Rule-065")]
        hits = all_violations(tree)
        if not hits:
            return []
        violations: list[ViolationRecord] = []
        for lineno, label in hits:
            violations.append(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id="Rule-065",
                resource=f"{file_path}:{lineno}",
                message=(
                    f"{label}. Every LLM call goes through the pydantic-ai "
                    f"framework via the ChatHealthy LLM facade "
                    f"(chathealthy_lib.llm run_llm/run_llm_sync over a "
                    f"pydantic_ai.Agent). No agent exists outside that framework."
                ),
            ))
        return violations


def main() -> int:
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-011"
    return ScanLlmFacadeEnforcementWorker(enforcement_id).run()


if __name__ == "__main__":
    sys.exit(main())
