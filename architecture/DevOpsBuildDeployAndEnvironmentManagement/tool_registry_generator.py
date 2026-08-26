# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Derives the tool registry from source at build time.

A model can only be told a tool exists if something knows it exists before
anything calls it. Every runtime answer fails that: a tool is discovered by
being imported, and it is imported by the code that already knew to reach
it. So the registry is read out of the source rather than out of a running
process -- no imports, no API keys, no execution.

It is generated per build from the same tree the build is made from, so it
cannot name a tool the build does not carry and cannot miss one it does.

The pipeline tree is NOT walked. Its work reaches models five different
ways and none of them is a tool, so a registry generated from it would come
back empty -- which reads as "the pipeline has no tools" rather than "the
pipeline's tools have not been written". An absent registry shows the gap;
an empty one certifies it away.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Optional

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

BASE_CLASS = "ChatHealthyTool"

FRONT_END_ROOTS = ("sharedServices", "FindCare", "Code", "evaluateCare")

# Named, not merely left out. A tree that is absent from FRONT_END_ROOTS
# could be absent because nobody thought of it; a tree that is refused here
# is absent because someone decided. The generator raises if one of these
# is ever passed as a root, so the decision cannot be undone by accident.
EXCLUDED_ROOTS = {
    "pipeline": "The pipeline reaches models five ways and none of them is "
                "a tool, so a registry generated from it would come back "
                "empty -- which reads as 'the pipeline has no tools' rather "
                "than 'the pipeline's tools have not been written'. Walking "
                "it would certify the gap away.",
}

EXCLUDED_PARTS = ("build", "localBuild", "node_modules", "__pycache__",
                  "_oneshots", "_port_out", "tests")


def _is_tool(node: ast.ClassDef) -> bool:
    return any(getattr(base, "id", "") == BASE_CLASS for base in node.bases)


def _tool_name(node: ast.ClassDef) -> Optional[str]:
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if getattr(target, "id", "") == "TOOL_NAME":
                value = stmt.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
    return None


def _description(node: ast.AnnAssign) -> str:
    """The Field(description=...) on one model field, if it carries one."""
    if not isinstance(node.value, ast.Call):
        return ""
    for keyword in node.value.keywords:
        if keyword.arg != "description":
            continue
        try:
            return str(ast.literal_eval(keyword.value))
        except ValueError:
            return ""
    return ""


def _fields(node: ast.ClassDef) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or stmt.target is None:
            continue
        name = getattr(stmt.target, "id", None)
        if name is None:
            continue
        out.append({
            "name": name,
            "type": ast.unparse(stmt.annotation),
            "required": stmt.value is None,
            "description": _description(stmt),
        })
    return out


def _model(tree: ast.Module, name: str, path: Path,
           repo_root: Path) -> list[dict[str, Any]]:
    """The named model's fields, whether it is defined here or imported.

    A tool that imports its Request and Response from a models module is
    still a tool with a contract. Reading only the file the class sits in
    reported clinical_trials as having no input and no output, which is the
    registry going quiet about a tool rather than describing it.
    """
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return _fields(node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not any(alias.name == name for alias in node.names):
            continue
        source = _resolve_module(node.module, path, repo_root)
        if source is None:
            continue
        try:
            imported = ast.parse(source.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for inner in imported.body:
            if isinstance(inner, ast.ClassDef) and inner.name == name:
                return _fields(inner)
    return []


def _resolve_module(module: str, importer: Path,
                    repo_root: Path) -> Optional[Path]:
    """Find the file a dotted module name refers to.

    Tried beside the importing file first, then from each front-end root,
    because these modules are imported under more than one package prefix
    depending on which container is running.
    """
    tail = Path(*module.split("."))
    candidates = [importer.parent / tail.name,
                  importer.parent.parent / tail]
    candidates.extend(repo_root / root / tail for root in FRONT_END_ROOTS)
    candidates.append(repo_root / tail)
    for candidate in candidates:
        as_file = candidate.with_suffix(".py")
        if as_file.is_file():
            return as_file
    return None


_HEADER = '''# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# GENERATED AT BUILD TIME. Do not edit; edit the tools.
#
# Read out of the source tree this build was made from, so it names every
# tool the build carries and no others. Nothing here was imported and
# nothing was called to produce it.
"""The tools this build carries, and their contracts."""
from __future__ import annotations

TOOLS = {tools!r}


class ToolRegistry:
    """The tools, and what goes in and comes out of each."""

    @staticmethod
    def tools() -> list[dict]:
        return list(TOOLS)

    @staticmethod
    def names() -> list[str]:
        return [tool["tool_name"] for tool in TOOLS]

    @staticmethod
    def _render(side: str) -> str:
        blocks = []
        for tool in TOOLS:
            fields = tool[side]
            if not fields:
                continue
            lines = [f"{{tool['tool_name']}}:"]
            for field in fields:
                need = "required" if field["required"] else "optional"
                text = field["description"] or "(undocumented)"
                lines.append(f"  {{field['name']}} ({{need}}) — {{text}}")
            blocks.append("\\n".join(lines))
        return "\\n\\n".join(blocks)

    @classmethod
    def jsons_in(cls) -> str:
        """What each tool must be given."""
        return cls._render("input")

    @classmethod
    def jsons_out(cls) -> str:
        """What each tool gives back."""
        return cls._render("output")
'''


def write_registry(repo_root: Path, destination: Path) -> int:
    """Generate the registry module. Returns how many tools it names."""
    tools = read_tools(repo_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_HEADER.format(tools=tools), encoding="utf-8")
    return len(tools)


def read_tools(repo_root: Path) -> list[dict[str, Any]]:
    """Every front-end tool, its input model and its output model.

    Ordered by tool name so a build produces the same registry twice.
    """
    tools: list[dict[str, Any]] = []
    for root in FRONT_END_ROOTS:
        if root in EXCLUDED_ROOTS:
            raise ChatHealthyException(
                mode="tool_registry_excluded_root",
                component="ToolRegistryGenerator",
                message=f"{root!r} is excluded from the tool registry and "
                        f"MUST NOT be walked. {EXCLUDED_ROOTS[root]}",
            )
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in tree.body:
                if not (isinstance(node, ast.ClassDef) and _is_tool(node)):
                    continue
                name = _tool_name(node)
                if name is None:
                    raise ChatHealthyException(
                        mode="tool_registry_tool_unnamed",
                        component="ToolRegistryGenerator",
                        message=f"{path.relative_to(repo_root).as_posix()}: "
                                f"{node.name} subclasses {BASE_CLASS} and "
                                f"declares no TOOL_NAME, so nothing can name "
                                f"it to a model.",
                    )
                tools.append({
                    "tool_name": name,
                    "class_name": node.name,
                    "source_location": path.relative_to(repo_root).as_posix(),
                    "docstring": (ast.get_docstring(node) or "").strip(),
                    "input": _model(tree, "Request", path, repo_root),
                    "output": _model(tree, "Response", path, repo_root),
                })
    return sorted(tools, key=lambda t: t["tool_name"])
