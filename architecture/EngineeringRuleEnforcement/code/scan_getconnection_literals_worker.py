"""scan_getconnection_literals_worker.py — getConnection literal-pair enforcement.

The mandate: every getConnection(identity, cluster) and set_mongo_log_identity(
identity) call outside the canonical connection plumbing is written with
hardcoded STRING LITERALS -- never a variable, constant, attribute, or alias --
and each literal names a real Atlas DB user / a real Atlas cluster. The purpose
is that every DB connection is greppable: the reader sees, at the call site,
exactly which identity opens which cluster. This matters now that each identity
is least-privilege-scoped in Mongo.

Two checks:
  1. LITERAL (always, offline, AST): each identity/cluster argument is a string
     literal. A variable/constant/attribute/subscript is an alias and fails.
  2. KNOWN (best-effort, Atlas Admin API): each literal identity names a DB user
     and each literal cluster names a cluster that exists in the firm's project.
     If Atlas is unreachable, the literal check still stands and one non-fatal
     line records that the names could not be verified.

The plumbing (mongo_utilities and the thin wrappers that take identity/cluster
as parameters) is exempted through this enforcement's SCOPES, not in code.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import urllib.error
import urllib.request

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND, ChatHealthyException,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, PROJECT_ROOT,
        EXIT_OK, EXIT_VIOLATIONS_FOUND, ChatHealthyException,
    )

_RULE_ID = "Rule-065"
_WATCHED = frozenset({"getConnection", "set_mongo_log_identity"})
_ATLAS_BASE = "https://cloud.mongodb.com/api/atlas/v2"
_ATLAS_ACCEPT = "application/vnd.atlas.2023-11-15+json"


def _call_name(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _as_str_literal(node: ast.AST) -> str | None:
    """The string value if the node is a bare string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _render(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return type(node).__name__


class _AtlasFacts:
    """Real cluster names and DB-user identities for the firm's project, read
    once from the Atlas Admin API. Short-CN identities are derived from each
    user's X.509 DN (CN=<identity>,...). None on either set means the API could
    not answer; the caller treats that as 'cannot verify', never as 'empty'."""

    def __init__(self) -> None:
        self.clusters: set[str] | None = None
        self.identities: set[str] | None = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            from dotenv import load_dotenv  # noqa: PLC0415
            load_dotenv(PROJECT_ROOT / ".env")
        except Exception:  # noqa: BLE001
            pass
        pub = os.environ.get("ATLAS_PUBLIC_KEY", "").strip()
        priv = os.environ.get("ATLAS_PRIVATE_KEY", "").strip()
        proj = os.environ.get("ATLAS_PROJECT_ID", "").strip()
        if not (pub and priv and proj):
            self.error = "ATLAS_PUBLIC_KEY/ATLAS_PRIVATE_KEY/ATLAS_PROJECT_ID absent"
            return
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, "https://cloud.mongodb.com", pub, priv)
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))

        def get(path: str):
            req = urllib.request.Request(_ATLAS_BASE + path, headers={"Accept": _ATLAS_ACCEPT})
            with opener.open(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            cl = get(f"/groups/{proj}/clusters")
            self.clusters = {c.get("name") for c in cl.get("results", []) if c.get("name")}
            du = get(f"/groups/{proj}/databaseUsers")
            idents: set[str] = set()
            for u in du.get("results", []):
                name = u.get("username", "")
                idents.add(name)
                if name.startswith("CN="):
                    idents.add(name[3:].split(",", 1)[0])  # short CN
            self.identities = idents
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"


class ScanGetConnectionLiteralsEnforcementWorker(EnforcementWorker):
    """Every getConnection/set_mongo_log_identity uses hardcoded, real literals."""

    SCOPE_DEFAULTS: dict[str, bool] = {"_scan_getconnection_literals": False}
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0
        self._atlas: _AtlasFacts | None = None
        self._atlas_note_emitted: bool = False

    def _facts(self) -> _AtlasFacts:
        if self._atlas is None:
            self._atlas = _AtlasFacts()
        return self._atlas

    def run(self) -> int:
        any_violations = False
        for file_path in self.files:
            self.files_scanned += 1
            if not self.is_in_scope(file_path, "_scan_getconnection_literals"):
                continue
            for v in self._scan_getconnection_literals(file_path):
                self._emit_violation(v)
                self.violation_count += 1
                any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _scan_getconnection_literals(self, file_path: str) -> list[ViolationRecord]:
        absolute = (PROJECT_ROOT / file_path).resolve()
        if not absolute.is_file() or not file_path.endswith(".py"):
            return []
        try:
            source = self.read_text(file_path)
        except ChatHealthyException as exc:
            return [self.uncertifiable_violation(file_path, exc, rule_id=_RULE_ID)]
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [ViolationRecord(
                enforcement_id=self.enforcement_id, rule_id=_RULE_ID,
                resource=file_path,
                message=(f"file could not be parsed ({type(exc).__name__}: "
                         f"{str(exc)[:120]}); a file that cannot be scanned cannot "
                         f"be certified compliant."),
                severity="error")]
        violations: list[ViolationRecord] = []
        facts = self._facts()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in _WATCHED:
                continue
            if name == "getConnection":
                if len(node.args) < 2:
                    continue
                violations += self._check(file_path, node.lineno, node.args[0], "identity", facts)
                violations += self._check(file_path, node.lineno, node.args[1], "cluster", facts)
            else:  # set_mongo_log_identity
                if not node.args:
                    continue
                violations += self._check(file_path, node.lineno, node.args[0], "identity", facts)
        return violations

    def _check(self, file_path, lineno, arg, kind, facts) -> list[ViolationRecord]:
        lit = _as_str_literal(arg)
        if lit is None:
            return [ViolationRecord(
                enforcement_id=self.enforcement_id, rule_id=_RULE_ID,
                resource=f"{file_path}:{lineno}",
                message=(f"the {kind} passed to this connection is not a string "
                         f"literal: `{_render(arg)}`. Every getConnection / "
                         f"set_mongo_log_identity call MUST hardcode the identity "
                         f"and cluster as literals so the call site plainly states "
                         f"which identity opens which cluster -- no variable, "
                         f"constant, or alias. Inline the literal value here."),
                severity="error")]
        known = facts.identities if kind == "identity" else facts.clusters
        if known is None:
            self._note_atlas_unverified(facts)
            return []
        if lit not in known:
            kinds = "Atlas DB user" if kind == "identity" else "Atlas cluster"
            return [ViolationRecord(
                enforcement_id=self.enforcement_id, rule_id=_RULE_ID,
                resource=f"{file_path}:{lineno}",
                message=(f"the {kind} literal {lit!r} does not name a real "
                         f"{kinds} in the firm's Atlas project. A connection can "
                         f"only open as an identity that exists and against a "
                         f"cluster that exists; a name that is neither is a typo "
                         f"or an alias. Known: {sorted(known)}."),
                severity="error")]
        return []

    def _note_atlas_unverified(self, facts) -> None:
        if self._atlas_note_emitted:
            return
        self._atlas_note_emitted = True
        self._emit_violation(ViolationRecord(
            enforcement_id=self.enforcement_id, rule_id=_RULE_ID,
            resource="(atlas)",
            message=(f"NOTE: identity/cluster names could not be verified against "
                     f"Atlas ({facts.error}); the literal check still applies. "
                     f"This note does not fail the commit."),
            severity="warning"))


def main(argv: list[str] | None = None) -> int:
    return ScanGetConnectionLiteralsEnforcementWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
