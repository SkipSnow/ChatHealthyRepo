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
import json
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

# The five call types the target composition map recognises. Four of them
# name a route the navigator answers from outside (they carry a proof the
# navigator verifies): client_sessioned, third_party_callback,
# internal_callback, static_witness. The fifth, tool_to_tool, is not a
# route at all -- it is the MAY_CALL graph, one tool reaching another by
# TOOL.run(), and it carries no route entry. So the record's routes[] name
# only the first four today; tool_to_tool is realised entirely by MAY_CALL.
# (The refactor plan Phase 4 records tool_to_tool as "absent from the
# record today"; it is named here so the enumeration is complete, not so a
# route is invented for it.)
CALL_TYPES = (
    "client_sessioned",
    "third_party_callback",
    "internal_callback",
    "static_witness",
    "tool_to_tool",
)

# Where the record's routes[] live: the ToolConfiguration record on the
# front-end Atlas target. Read from the git-tracked manifest at build time,
# never from the live Mongo collection -- the same static-source discipline
# the tool walk uses.
_MANIFEST_RELPATH = ("brain", "machine_artifacts", "content",
                     "deployment_architecture.json")
_ROUTES_TARGET_ID = "target_atlas_frontend"

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


def _class_assignments(node: ast.ClassDef):
    """Yield (attr_name, value_node) for every class-body assignment.

    Covers both the plain `TOOL_NAME = ...` (Assign) and the annotated
    `SUBSCRIPTIONS: list[str] = ...` (AnnAssign) forms the tools use.
    """
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign):
            name = getattr(stmt.target, "id", None)
            if name is not None and stmt.value is not None:
                yield name, stmt.value
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                name = getattr(target, "id", None)
                if name is not None:
                    yield name, stmt.value


def _str_list_attr(node: ast.ClassDef, attr: str) -> Optional[list[str]]:
    """The value of a class attribute declared as a list of string literals.

    Returns None when the attribute is not declared at all -- distinct from
    an empty list, which the tool contract treats as "declared nothing".
    Raises if the attribute is present but is not a plain list of strings,
    because a MAY_CALL or SUBSCRIPTIONS the build cannot read statically is a
    fact the registry would go silent about.
    """
    for name, value in _class_assignments(node):
        if name != attr:
            continue
        if not isinstance(value, ast.List):
            raise ChatHealthyException(
                mode="tool_registry_declaration_not_static",
                component="ToolRegistryGenerator",
                message=f"{node.name}.{attr} is not a list literal the build "
                        f"can read from source.",
            )
        items: list[str] = []
        for element in value.elts:
            if not (isinstance(element, ast.Constant)
                    and isinstance(element.value, str)):
                raise ChatHealthyException(
                    mode="tool_registry_declaration_not_static",
                    component="ToolRegistryGenerator",
                    message=f"{node.name}.{attr} holds a non-string-literal "
                            f"entry the build cannot read from source.",
                )
            items.append(element.value)
        return items
    return None


def _routable(node: ast.ClassDef) -> bool:
    """Whether a person's free-text utterance may route to this tool.

    Reads the two mutually-exclusive routability declarations exactly as the
    ChatHealthyTool base class validates them: a non-empty
    UTTERANCE_MANAGER_PROMPT means routable; NOT_UTTERANCE_ROUTABLE = True
    means it is not. The base class already refuses a subclass that declares
    neither or both, so this only reports which of the two was chosen.
    """
    not_routable = False
    has_prompt = False
    for name, value in _class_assignments(node):
        if name == "NOT_UTTERANCE_ROUTABLE":
            not_routable = isinstance(value, ast.Constant) and value.value is True
        elif name == "UTTERANCE_MANAGER_PROMPT":
            has_prompt = True
    return has_prompt and not not_routable


def _tool_description(node: ast.ClassDef) -> str:
    """The one-sentence TOOL_DESCRIPTION when it is a plain string literal.

    Some tools set it to a module-level name (e.g. a prompt constant) the
    build cannot resolve without executing the module; those keep the empty
    string here. The dispatch registry does not depend on the sentence text.
    """
    for name, value in _class_assignments(node):
        if name != "TOOL_DESCRIPTION":
            continue
        try:
            resolved = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return ""
        return resolved if isinstance(resolved, str) else ""
    return ""


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


def build_dispatch_table(tools: list[dict[str, Any]]) -> dict[str, list[str]]:
    """op/route -> the tools subscribing to it. Presence IS the allowlist.

    Read from each tool's SUBSCRIPTIONS. A gate op the navigator may
    dispatch is exactly an op some tool subscribes to; nothing else is
    dispatchable. Today most ops resolve to a single subscriber, but the
    table is a list per op so a second subscriber is a fact the navigator
    reads and refuses, not one the build hides.
    """
    table: dict[str, list[str]] = {}
    for tool in tools:
        for op in tool["subscriptions"]:
            table.setdefault(op, []).append(tool["tool_name"])
    return {op: sorted(names) for op, names in sorted(table.items())}


def build_may_call_graph(tools: list[dict[str, Any]]) -> dict[str, list[str]]:
    """tool -> the tools it may call directly. The declared call graph.

    Read from each tool's MAY_CALL. Every tool appears as a key (even those
    that call nothing, with an empty list) so a reader never has to tell
    "declared no callees" apart from "not a tool".
    """
    return {tool["tool_name"]: list(tool["may_call"])
            for tool in sorted(tools, key=lambda t: t["tool_name"])}


def read_routes(repo_root: Path) -> list[dict[str, Any]]:
    """The declared routes[] and their call_type, read from the manifest.

    The routes live on the ToolConfiguration record of the front-end Atlas
    target, one copy per environment. This reads the git-tracked manifest --
    NOT the live Mongo collection the deploy writes that same record into --
    and requires every environment's copy to be identical, raising if they
    diverge rather than silently choosing one. Each route's call_type must be
    one of CALL_TYPES.

    N20 [OPERATOR]: this bakes a build-time snapshot of routes that ALSO has
    a live-collection home (ChatHealthyConfig.ToolConfiguration, deploy-
    written). Which of the two the runtime treats as authoritative is the
    unresolved data-version-authority question the refactor plan routes to
    the operator. This generator emits the snapshot and does not resolve it.
    """
    manifest_path = repo_root.joinpath(*_MANIFEST_RELPATH)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = None
    for entry in manifest.get("DeploymentTargetRecord", []):
        if entry.get("target_id") == _ROUTES_TARGET_ID:
            target = entry
            break
    if target is None:
        raise ChatHealthyException(
            mode="tool_registry_routes_target_missing",
            component="ToolRegistryGenerator",
            message=f"manifest has no DeploymentTargetRecord for "
                    f"{_ROUTES_TARGET_ID!r}, so routes[] cannot be read.",
        )

    per_env: list[list[dict[str, Any]]] = []
    for environment in target.get("environments", []):
        for coll in environment.get("config_collections", []):
            for record in coll.get("records", []):
                if "routes" in record:
                    per_env.append(record["routes"])
    if not per_env:
        raise ChatHealthyException(
            mode="tool_registry_routes_absent",
            component="ToolRegistryGenerator",
            message=f"{_ROUTES_TARGET_ID!r} declares no routes[] on any "
                    f"ToolConfiguration record.",
        )
    canonical = per_env[0]
    for other in per_env[1:]:
        if other != canonical:
            raise ChatHealthyException(
                mode="tool_registry_routes_env_divergence",
                component="ToolRegistryGenerator",
                message="routes[] differ between environments; the registry "
                        "is env-agnostic and cannot pick one. Reconcile the "
                        "ToolConfiguration records or make the registry "
                        "env-scoped.",
            )
    routes: list[dict[str, Any]] = []
    for route in canonical:
        call_type = route.get("call_type")
        if call_type not in CALL_TYPES:
            raise ChatHealthyException(
                mode="tool_registry_route_unknown_call_type",
                component="ToolRegistryGenerator",
                message=f"route {route.get('route_name')!r} declares "
                        f"call_type {call_type!r}, which is not one of "
                        f"{CALL_TYPES}.",
            )
        routes.append({
            "route_name": route.get("route_name"),
            "call_type": call_type,
            "proof": route.get("proof", {}),
        })
    return sorted(routes, key=lambda r: r["route_name"] or "")


_HEADER = '''# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# GENERATED AT BUILD TIME. Do not edit; edit the tools.
#
# Read out of the source tree this build was made from, so it names every
# tool the build carries and no others. Nothing here was imported and
# nothing was called to produce it.
"""The tools this build carries, their contracts, and how they compose."""
from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException

TOOLS = {tools!r}

# op/route -> the tool(s) subscribing to it. Presence is the allowlist: an
# op absent here is an op no tool answers, and the navigator refuses it.
DISPATCH_TABLE = {dispatch_table!r}

# tool -> the tools it may call directly (the declared tool_to_tool graph).
MAY_CALL = {may_call!r}

# route_name -> {{call_type, proof}}. Read from the manifest's
# ToolConfiguration record at build; a build-time snapshot of a record that
# also has a live-collection home (N20, unresolved -- see the generator).
ROUTES = {routes!r}

# The five call types the composition map recognises. tool_to_tool is the
# MAY_CALL graph and names no route; the other four each name routes above.
CALL_TYPES = {call_types!r}


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


class DispatchRegistry:
    """How the tools this build carries compose: what answers each op, which
    tool may call which, and the transport route of each declared entrance.

    Read out of the build-generated tables above -- nothing here is imported
    or executed. This is the read side of the composition map the navigator
    and the tools consult; it does NOT itself dispatch. Wiring the navigator
    and the tool call sites onto it is a later phase.
    """

    @staticmethod
    def subscribers_for(op):
        """The tool(s) that subscribe to one op/route. [] when none do."""
        return list(DISPATCH_TABLE.get(op, []))

    @staticmethod
    def dispatch_table():
        """The whole op -> subscribing-tool(s) table."""
        return {{op: list(names) for op, names in DISPATCH_TABLE.items()}}

    @staticmethod
    def ops():
        """Every op some tool subscribes to."""
        return sorted(DISPATCH_TABLE)

    @staticmethod
    def may_call_targets(tool):
        """The tools one tool may call directly. [] when it calls none.

        Raises for a name that is not a tool this build carries, so a
        caller cannot ask the graph about something the build never saw.
        """
        if tool not in MAY_CALL:
            raise ChatHealthyException(
                mode="tool_not_in_build",
                message="may_call_targets: %r is not a tool in this build" % (tool,))
        return list(MAY_CALL[tool])

    @staticmethod
    def may_call(caller, callee):
        """Whether caller is declared to be allowed to call callee."""
        return callee in MAY_CALL.get(caller, [])

    @staticmethod
    def may_call_graph():
        """The whole tool -> may-call-targets graph."""
        return {{tool: list(targets) for tool, targets in MAY_CALL.items()}}

    @staticmethod
    def route(route_name):
        """One route's {{route_name, call_type, proof}}, or None."""
        for entry in ROUTES:
            if entry.get("route_name") == route_name:
                return dict(entry)
        return None

    @staticmethod
    def call_type_of(route_name):
        """One route's call_type, or None when the route is not declared."""
        entry = DispatchRegistry.route(route_name)
        return entry.get("call_type") if entry else None

    @staticmethod
    def routes():
        """Every declared route with its call_type and proof."""
        return [dict(entry) for entry in ROUTES]

    @staticmethod
    def call_types():
        """The five recognised call types."""
        return list(CALL_TYPES)
'''


def write_registry(repo_root: Path, destination: Path) -> int:
    """Generate the registry module. Returns how many tools it names.

    One generated module now carries both the tool contracts (ToolRegistry)
    and the composition map (DispatchRegistry): the op -> tool dispatch
    table, the may_call graph, and the declared routes with their call
    types. The two registries the refactor plan found standing apart are
    emitted here as one document from one build.
    """
    tools = read_tools(repo_root)
    dispatch_table = build_dispatch_table(tools)
    may_call = build_may_call_graph(tools)
    routes = read_routes(repo_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        _HEADER.format(
            tools=tools,
            dispatch_table=dispatch_table,
            may_call=may_call,
            routes=routes,
            call_types=CALL_TYPES,
        ),
        encoding="utf-8",
    )
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
                subscriptions = _str_list_attr(node, "SUBSCRIPTIONS")
                may_call = _str_list_attr(node, "MAY_CALL")
                tools.append({
                    "tool_name": name,
                    "class_name": node.name,
                    "source_location": path.relative_to(repo_root).as_posix(),
                    "docstring": (ast.get_docstring(node) or "").strip(),
                    "description": _tool_description(node),
                    "subscriptions": subscriptions or [],
                    "may_call": may_call or [],
                    "utterance_routable": _routable(node),
                    "input": _model(tree, "Request", path, repo_root),
                    "output": _model(tree, "Response", path, repo_root),
                })
    return sorted(tools, key=lambda t: t["tool_name"])
