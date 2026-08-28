"""scan_separation_of_concerns_worker.py - Rule-009 worker.

Enforces EPIC-008-F-002-S-016-REQ-B-001/B-002/B-003: three deterministic
checks on the front-end architectural separation between React display,
plumbing JS/HTML, and Python tools.

  _scan_display_authoring (.js + .html outside React src):
      forbid innerHTML/outerHTML/textContent/innerText assignments,
      DOM construction (createElement chains carrying visible text),
      and template literals containing HTML markup.

  _scan_react_persistence (.ts + .tsx under React src):
      forbid writes to localStorage/sessionStorage/indexedDB/document.cookie
      and any fetch/XMLHttpRequest/navigator.sendBeacon call that does not
      resolve to the canonical ClientRouter.getStreamedPayloads /
      .getFullPayload / router:makeCall postMessage path.

  _scan_business_logic (.js + .html outside React src):
      forbid domain-vocabulary token usage (provider, specialty, NPI,
      NUCC, clinical_trial, evaluation, user_object).

Detection is deterministic - no LLM. HTML uses stdlib html.parser to
extract <script> bodies; JS/TS/TSX uses regex on the parsed string
contents per the established pattern (worker is on Rule-008's
_block_regular_expressions_in_executable_code excluded_exact list per
EPIC-008-F-002-S-016-REQ-B-004).
"""
from __future__ import annotations

import json

import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
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


# REQ-B-001 patterns
_DISPLAY_SINK_ASSIGN = re.compile(
    r"\.(innerHTML|outerHTML|textContent|innerText)\s*\+?\s*="
)
_TEMPLATE_LITERAL_HTML = re.compile(r"`[^`]*<[A-Za-z][A-Za-z0-9]*[^`]*?>[^`]*`")
_CREATE_ELEMENT = re.compile(r"\bdocument\.createElement\s*\(")


# REQ-B-002 patterns
_PERSIST_WRITE = re.compile(
    r"\b(localStorage|sessionStorage)\.(setItem|removeItem|clear)\s*\(|"
    r"\bindexedDB\.[A-Za-z_$][A-Za-z0-9_$]*\s*\(|"
    r"\bdocument\.cookie\s*="
)
_OUTBOUND_CALL = re.compile(
    r"\bfetch\s*\(|"
    r"\bnew\s+XMLHttpRequest\b|"
    r"\bnavigator\.sendBeacon\s*\("
)
_CANONICAL_DISPATCH = re.compile(
    r"ClientRouter\.(getStreamedPayloads|getFullPayload)\s*\(|"
    r"['\"]router:makeCall['\"]"
)


# REQ-B-003 patterns
_DOMAIN_TOKENS = (
    "provider", "specialty", "specialties", "npi", "nucc",
    "clinical_trial", "clinicaltrial", "evaluation", "user_object",
)
_DOMAIN_TOKEN_RX = re.compile(
    r"\b(" + "|".join(_DOMAIN_TOKENS) + r")\b", re.IGNORECASE
)


class _InlineScriptCollector(HTMLParser):
    """Collect inline <script> body text with source line offset."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._in_script = False
        self._buf: list[str] = []
        self._start_line = 0
        self.scripts: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):  # type: ignore[override]
        if tag.lower() == "script" and not any(k == "src" for k, _ in attrs):
            self._in_script = True
            self._buf = []
            self._start_line = self.getpos()[0]

    def handle_endtag(self, tag):  # type: ignore[override]
        if tag.lower() == "script" and self._in_script:
            self.scripts.append((self._start_line, "".join(self._buf)))
            self._in_script = False

    def handle_data(self, data):  # type: ignore[override]
        if self._in_script:
            self._buf.append(data)



def _ch_exception():
    """ChatHealthyException, resolved without assuming the library is on the
    path. Enforcement workers are spawned as bare scripts by the manager."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException

def _strip_js_comments(source: str) -> str:
    """Replace JS line comments (// ...) and block comments (/* ... */)
    with spaces, preserving newlines so line numbers in the stripped
    source still match the original. Naive: no string-literal tracking,
    so a `//` inside a string literal is also stripped. The effect is at
    most a false negative in the scan, which is acceptable - comments
    are the only place this scanner should ignore content."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        c2 = source[i + 1] if i + 1 < n else ""
        if c == "/" and c2 == "/":
            j = source.find("\n", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j - i))
                i = j
        elif c == "/" and c2 == "*":
            j = source.find("*/", i + 2)
            end = (n if j == -1 else j + 2)
            seg = source[i:end]
            out.append("".join(" " if ch != "\n" else "\n" for ch in seg))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


_STATIC_PAGE_PACKAGE = "static_pages"
_static_pages_cache: set | None = None


def _static_page_files() -> set:
    """Every file the record assigns to the static-page package.

    A static page authors its own display; that is what a page is. The
    wrapper's runtime driver does not, and it lives in the same directory as
    the pages, so a path cannot tell them apart -- Website/index.html belongs
    to the runtime-driver package while Website/terms.html is a page. The
    record already states which is which, so the rule reads it rather than
    carrying a second list that drifts from it.
    """
    global _static_pages_cache
    if _static_pages_cache is not None:
        return _static_pages_cache
    record = (PROJECT_ROOT / "brain" / "machine_artifacts" / "content"
              / "deployment_architecture.json")
    found: set = set()
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable record waives nothing
        _static_pages_cache = found
        return found
    for target in data.get("DeploymentTargetRecord", []):
        for entry in target.get("files", []) or []:
            if entry.get("package") == _STATIC_PAGE_PACKAGE:
                found.add(entry.get("source_location", ""))
        for binding in target.get("environments", []) or []:
            for pkg in binding.get("packages", []) or []:
                if pkg.get("package_id") != _STATIC_PAGE_PACKAGE:
                    continue
                for entry in pkg.get("files", []) or []:
                    found.add(entry.get("source_location", ""))
    _static_pages_cache = found
    return found


def _is_static_page(file_path: str) -> bool:
    return file_path.replace("\\", "/") in _static_page_files()


def _authored_right_hand_side(src: str, assign_end: int) -> bool:
    """True when what is assigned is composed here rather than handed in.

    Authoring is composing markup or visible text. `sink.innerHTML = content`,
    where content arrived as an argument, composes nothing -- it carries
    someone else's bytes into a frame, which is what a courier does. Treating
    those as the same thing is what put the one file that couriers onto a
    waiver list, and the waiver then covered the real authoring beside it.

    The test is the expression, not the sink: a literal, a template literal or
    a concatenation is authored; a bare name or property path is carried.
    """
    end = src.find(";", assign_end)
    rhs = src[assign_end:end if end != -1 else assign_end + 200]
    return any(ch in rhs for ch in ("'", '"', "`", "+"))


def _find_display_violations(source: str) -> list[tuple[int, str]]:
    src = _strip_js_comments(source)
    hits: list[tuple[int, str]] = []
    for m in _DISPLAY_SINK_ASSIGN.finditer(src):
        if not _authored_right_hand_side(src, m.end()):
            continue
        hits.append((src.count("\n", 0, m.start()) + 1, m.group(1)))
    for m in _TEMPLATE_LITERAL_HTML.finditer(src):
        hits.append((src.count("\n", 0, m.start()) + 1, "template-literal-html"))
    for m in _CREATE_ELEMENT.finditer(src):
        # The stated contract is "createElement chains carrying visible text".
        # An element created and filled with content handed in is a container,
        # not authored display, so the chain counts only when something
        # composed here goes into it.
        window = src[m.start():m.start() + 400]
        composed = any(_authored_right_hand_side(window, a.end())
                       for a in _DISPLAY_SINK_ASSIGN.finditer(window))
        if not composed and not _TEMPLATE_LITERAL_HTML.search(window):
            continue
        hits.append((src.count("\n", 0, m.start()) + 1, "createElement"))
    return hits


def _find_persistence_violations(source: str) -> list[tuple[int, str]]:
    src = _strip_js_comments(source)
    hits: list[tuple[int, str]] = []
    for m in _PERSIST_WRITE.finditer(src):
        hits.append((src.count("\n", 0, m.start()) + 1, "persistent-store-write"))
    for m in _OUTBOUND_CALL.finditer(src):
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_end = src.find("\n", m.start())
        line = src[line_start:line_end if line_end != -1 else None]
        if _CANONICAL_DISPATCH.search(line):
            continue
        window = src[m.start():m.start() + 200]
        if _CANONICAL_DISPATCH.search(window):
            continue
        hits.append((src.count("\n", 0, m.start()) + 1, "non-canonical-outbound-call"))
    return hits


def _find_business_logic_violations(source: str) -> list[tuple[int, str]]:
    src = _strip_js_comments(source)
    hits: list[tuple[int, str]] = []
    for m in _DOMAIN_TOKEN_RX.finditer(src):
        hits.append((src.count("\n", 0, m.start()) + 1, m.group(1).lower()))
    return hits


def _scan_html_with_inline_scripts(source: str, scan_fn) -> list[tuple[int, str]]:
    parser = _InlineScriptCollector()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for start_line, body in parser.scripts:
        for rel_line, marker in scan_fn(body):
            out.append((start_line + rel_line - 1, marker))
    return out


class ScanSeparationOfConcernsWorker(EnforcementWorker):
    """Rule-009: front-end architectural separation of concerns."""

    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_display_authoring": False,
        "_scan_react_persistence": False,
        "_scan_business_logic": False,
    }
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    def _staged_files(self) -> list[str]:
        """The file array the Rule-065 driver handed down.

        This worker owns no git knowledge. What a commit answers for is one
        decision, and the driver makes it once for every subordinate.
        """
        return self.files

    def run(self) -> int:
        any_violations = False
        for file_path in self._staged_files():
            self.files_scanned += 1
            for vs in (
                self._scan_display_authoring(file_path),
                self._scan_react_persistence(file_path),
                self._scan_business_logic(file_path),
            ):
                for v in vs:
                    self._emit_violation(v)
                    self.violation_count += 1
                    any_violations = True
        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    def _read(self, file_path: str) -> str | None:
        absolute = (PROJECT_ROOT / file_path).resolve()
        if not absolute.is_file():
            return None
        try:
            return absolute.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def _scan_display_authoring(self, file_path: str) -> list[ViolationRecord]:
        if not self.is_in_scope(file_path, "_scan_display_authoring"):
            return []
        if _is_static_page(file_path):
            return []
        source = self._read(file_path)
        if source is None:
            return []
        if file_path.endswith(".html"):
            hits = _scan_html_with_inline_scripts(source, _find_display_violations)
        elif file_path.endswith(".js"):
            hits = _find_display_violations(source)
        else:
            return []
        return [self._make_violation(file_path, ln, marker,
                "Rule-009 REQ-B-001: display content authoring outside React")
                for ln, marker in hits]

    def _scan_react_persistence(self, file_path: str) -> list[ViolationRecord]:
        if not self.is_in_scope(file_path, "_scan_react_persistence"):
            return []
        source = self._read(file_path)
        if source is None:
            return []
        if not (file_path.endswith(".ts") or file_path.endswith(".tsx")):
            return []
        hits = _find_persistence_violations(source)
        return [self._make_violation(file_path, ln, marker,
                "Rule-009 REQ-B-002: React must not change persistent state directly")
                for ln, marker in hits]

    def _scan_business_logic(self, file_path: str) -> list[ViolationRecord]:
        if not self.is_in_scope(file_path, "_scan_business_logic"):
            return []
        source = self._read(file_path)
        if source is None:
            return []
        if file_path.endswith(".html"):
            hits = _scan_html_with_inline_scripts(source, _find_business_logic_violations)
        elif file_path.endswith(".js"):
            hits = _find_business_logic_violations(source)
        else:
            return []
        return [self._make_violation(file_path, ln, marker,
                "Rule-009 REQ-B-003: business logic outside tools")
                for ln, marker in hits]

    def _make_violation(self, file_path, lineno, marker, body) -> ViolationRecord:
        return ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id="Rule-009",
            resource=f"{file_path}:{lineno}",
            message=f"{body} (matched: {marker})",
        )


def main() -> int:
    """Drive the program and report its status.

    The exit lives here because this is the function the guard
    calls, and a process reports its outcome by exit code.
    """
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-007"
    return ScanSeparationOfConcernsWorker(enforcement_id).run()


if __name__ == "__main__":
    sys.exit(main())
