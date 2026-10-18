# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EPIC-008-F-002-S-013-REQ-B-002 (Rule-003) -- the one sanctioned program that
writes the agile backlog, the firm's requirements.

BUSINESS PROCESS. The operator and Claude work out the splice; the operator
closes the editor and reopens it elevated; Claude calls this program with the
splice. Then, in one run:

  A. Called with the FULL OBJECTS to splice -- each a complete epic, feature,
     story or requirement, self-identifying by its id. (Not a diff, not a
     half-object.)
  B. Compose the new file by splicing those objects into the current backlog,
     and CACHE the old file.
  C. Get the operator's authorization, showing the changed objects whole. Store
     the authorization WITH the new content, before any write.
  D. Replace the backlog with the composed new file.
  E. QA the actual written file -- re-read it, validate it against the schema,
     and confirm it is exactly the authorized content.
       pass -> F: delete the cached old and cached new. Done.
       fail -> F: roll back to the cached old, pop an "it failed QA" notice, roll
               back the authorization, and stop. The operator tries again.

ELEVATION. The backlog is OS read-only to non-administrators, so the write lands
only when the writing process is elevated. Authorization comes FIRST: the
operator approves the change and the authorization is recorded, and only THEN is
the write attempted. This session is normally not elevated, so the direct write
is refused by the OS; the write is then retried through a one-shot UAC-elevated
re-launch of this same program (--apply-write), so the operator elevates AFTER
approving, for this one write only. If the operator declines that prompt, the
authorization is rolled back and nothing changes. The hard guarantee is the ACL,
not this program trusting itself.
"""
from __future__ import annotations

import argparse
import copy
import json
import secrets
import shutil
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
for _candidate in _HERE.parents:
    _lib = _candidate / "ChatHealthyLib" / "src"
    if _lib.is_dir():
        sys.path.insert(0, str(_lib))
        break

from chathealthy_lib import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

import authorization_record  # noqa: E402
import rule_violation_popup  # noqa: E402

log = ChatHealthyLoggingService()

PROJECT_ROOT = next((p for p in _HERE.parents if (p / ".git").exists()),
                    _HERE.parents[-1])
TARGET = (PROJECT_ROOT / "brain" / "machine_artifacts" / "content"
          / "agile_backlog.json").resolve()
SCHEMA = (PROJECT_ROOT / "Website" / "schemas"
          / "ChatHealthyAgileBacklogSchema.json")
CACHE_DIR = PROJECT_ROOT / "_oneshots"
CACHE_OLD = CACHE_DIR / "agile_backlog.cache_old.json"
CACHE_NEW = CACHE_DIR / "agile_backlog.cache_new.json"

COMPONENT = "agile_backlog_editor"
REQUIREMENT = "EPIC-008-F-002-S-013-REQ-B-002"
AUTH_WINDOW_SECONDS = 600  # a 10-minute authorization window


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dumps(backlog: dict) -> str:
    return json.dumps(backlog, indent=2, ensure_ascii=False)


# ── composing the new file from full objects ─────────────────────────────────
# The backlog nests each level under a wrapper: epics.epic, features.feature,
# stories.story, requirements.requirement.
def _epic_list(backlog: dict) -> list:
    return (backlog.get("epics") or {}).get("epic", [])


def _feature_list(epic: dict) -> list:
    return (epic.get("features") or {}).get("feature", [])


def _story_list(feature: dict) -> list:
    return (feature.get("stories") or {}).get("story", [])


def _req_list(story: dict) -> list:
    return (story.get("requirements") or {}).get("requirement", [])


def _find_epic(backlog: dict, epic_id: str) -> dict | None:
    return next((e for e in _epic_list(backlog)
                 if e.get("epic_id") == epic_id), None)


def _find_feature(backlog: dict, feature_id: str) -> dict | None:
    epic = _find_epic(backlog, feature_id.rsplit("-F-", 1)[0])
    if epic is None:
        return None
    return next((f for f in _feature_list(epic)
                 if f.get("feature_id") == feature_id), None)


def _find_story(backlog: dict, story_id: str) -> dict | None:
    feature = _find_feature(backlog, story_id.rsplit("-S-", 1)[0])
    if feature is None:
        return None
    return next((s for s in _story_list(feature)
                 if s.get("story_id") == story_id), None)


def _replace_or_append(items: list, key: str, obj: dict) -> None:
    """Replace the item with the same id in place, or append when it is new."""
    target_id = obj.get(key)
    for index, existing in enumerate(items):
        if existing.get(key) == target_id:
            items[index] = obj
            return
    items.append(obj)


def _place(backlog: dict, obj: dict) -> None:
    """Splice one full object into the backlog by its id -- replacing the object
    with that id, or inserting it under the parent its id names."""
    if "req_id" in obj:
        story_id = obj["req_id"].rsplit("-REQ", 1)[0]
        story = _find_story(backlog, story_id)
        if story is None:
            raise ChatHealthyException(mode="splice_unplaceable",
                component=COMPONENT,
                message=f"requirement {obj['req_id']} names story {story_id}, "
                        "which is not in the backlog")
        items = story.setdefault("requirements", {}).setdefault("requirement", [])
        _replace_or_append(items, "req_id", obj)
    elif "story_id" in obj:
        feature_id = obj["story_id"].rsplit("-S-", 1)[0]
        feature = _find_feature(backlog, feature_id)
        if feature is None:
            raise ChatHealthyException(mode="splice_unplaceable",
                component=COMPONENT,
                message=f"story {obj['story_id']} names feature {feature_id}, "
                        "which is not in the backlog")
        items = feature.setdefault("stories", {}).setdefault("story", [])
        _replace_or_append(items, "story_id", obj)
    elif "feature_id" in obj:
        epic_id = obj["feature_id"].rsplit("-F-", 1)[0]
        epic = _find_epic(backlog, epic_id)
        if epic is None:
            raise ChatHealthyException(mode="splice_unplaceable",
                component=COMPONENT,
                message=f"feature {obj['feature_id']} names epic {epic_id}, "
                        "which is not in the backlog")
        items = epic.setdefault("features", {}).setdefault("feature", [])
        _replace_or_append(items, "feature_id", obj)
    elif "epic_id" in obj:
        items = backlog.setdefault("epics", {}).setdefault("epic", [])
        _replace_or_append(items, "epic_id", obj)
    else:
        raise ChatHealthyException(mode="splice_unplaceable",
            component=COMPONENT,
            message="object carries no epic/feature/story/req id: "
                    f"keys={sorted(obj)[:6]}")


def compose(current: dict, objects: list[dict]) -> dict:
    """The current backlog with every full object spliced in (new file)."""
    new = copy.deepcopy(current)
    for obj in objects:
        _place(new, obj)
    return new


# ── the changed objects, each whole ──────────────────────────────────────────
def _index_objects(backlog: dict) -> dict[str, dict]:
    """Every addressable object keyed by id, each the whole object."""
    out: dict[str, dict] = {}
    for epic in _epic_list(backlog):
        out[epic.get("epic_id", "?epic")] = {k: v for k, v in epic.items()
                                             if k != "features"}
        for feat in _feature_list(epic):
            out[feat.get("feature_id", "?feat")] = {k: v for k, v in feat.items()
                                                    if k != "stories"}
            for story in _story_list(feat):
                out[story.get("story_id", "?story")] = {
                    k: v for k, v in story.items() if k != "requirements"}
                for req in _req_list(story):
                    out[req.get("req_id", "?req")] = req
    return out


def changed_objects(current: dict, proposed: dict) -> list[dict]:
    """The objects that differ -- added or changed -- each in its entirety."""
    cur, prop = _index_objects(current), _index_objects(proposed)
    changed: list[dict] = []
    for oid, obj in prop.items():
        if (oid not in cur
                or json.dumps(cur[oid], sort_keys=True)
                != json.dumps(obj, sort_keys=True)):
            changed.append({"id": oid, "object": obj})
    return changed


def validate(backlog: dict) -> None:
    """Parseable JSON (already parsed) that conforms to the backlog schema."""
    import jsonschema
    jsonschema.validate(backlog, _load(SCHEMA))


# ── QA on the actual written file ────────────────────────────────────────────
def qa(target: Path, expected: dict) -> tuple[bool, str]:
    """Re-read the file now in place and confirm it is exactly the authorized
    content: parseable, schema-valid, equal to what was composed and approved."""
    try:
        actual = _load(target)
        validate(actual)
    except Exception as exc:  # noqa: BLE001
        return False, f"written file is not a valid backlog: {exc}"
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        return False, "written file does not match the authorized content"
    return True, ""


# ── the human ────────────────────────────────────────────────────────────────
def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _build_auth_server(changed: list[dict]):
    """Build (not start) the authorization server. Returns (server, url, token,
    state); server is None if the port could not be bound. Factored out so a
    test can drive the page and the approve/reject handshake -- including the
    human-evidence gate -- without a real browser."""
    body = "\n\n".join(
        f"// {c['id']}\n{json.dumps(c['object'], indent=2, ensure_ascii=False)}"
        for c in changed)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_urlsafe(8)
    state: dict = {"verdict": None, "evidence": {}}
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>Authorize agile backlog change</title><style>"
        "body{font-family:system-ui,sans-serif;padding:36px;background:#0b7a75;color:#fff}"
        "h1{font-size:24px;margin:0 0 6px}.warn{background:#7f1d1d;padding:10px 14px;"
        "border-radius:6px;font-weight:700;display:inline-block;margin-bottom:16px}"
        "pre{background:#04433f;padding:14px;border-radius:6px;white-space:pre-wrap;"
        "word-break:break-word;overflow:auto;max-height:460px;font-size:13px}"
        "button{font-size:18px;padding:14px 34px;margin:8px 8px 0 0;border:none;"
        "border-radius:6px;cursor:pointer;font-weight:700}.approve{background:#0f766e;color:#fff}"
        ".reject{background:#b91c1c;color:#fff}#why{margin:12px 0;color:#ffd7d5;min-height:20px}"
        "</style></head><body>"
        "<div class=warn>THIS CHANGES THE FIRM'S REQUIREMENTS</div>"
        "<h1>Please authorize this change to the agile backlog</h1>"
        f"<p>{len(changed)} object(s) changed, shown in full:</p>"
        f"<pre>{_escape(body)}</pre>"
        "<button class=approve id=a type=button>APPROVE</button>"
        "<button class=reject id=r type=button>REJECT</button><div id=why></div>"
        "<input type=hidden id=hc value=\"false\"><script>"
        f"var T='{token}';var moved=0,lx=null,ly=null,pressed=false,ev=0;"
        "document.addEventListener('mousemove',function(e){if(!e.isTrusted)return;ev++;"
        "if(lx!==null){moved+=Math.abs(e.clientX-lx)+Math.abs(e.clientY-ly);}lx=e.clientX;ly=e.clientY;"
        "if(moved>40&&pressed){document.getElementById('hc').value='true';}});"
        "document.addEventListener('mousedown',function(e){if(!e.isTrusted)return;pressed=true;"
        "if(moved>40){document.getElementById('hc').value='true';}});"
        "function s(v){var h=document.getElementById('hc').value;"
        "if(h!=='true'){document.getElementById('why').textContent='Move the mouse across this window, then click.';return;}"
        "fetch('/d',{method:'POST',body:new URLSearchParams({token:T,verdict:v,hc:h,ev:String(ev)})})"
        ".then(function(){document.body.innerHTML='<h1>Recorded.</h1>';try{window.close();}catch(e){}});}"
        "document.getElementById('a').addEventListener('click',function(){s('approve');});"
        "document.getElementById('r').addEventListener('click',function(){s('reject');});"
        "</script></body></html>")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): return
        def _send(self, code, b):
            raw = b.encode("utf-8"); self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):  # noqa: N802
            self._send(200, page) if urlparse(self.path).path == "/p" else self._send(404, "x")
        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/d":
                self._send(404, "x"); return
            n = int(self.headers.get("Content-Length", "0") or "0")
            f = parse_qs(self.rfile.read(n).decode("utf-8", "replace") if n else "")
            if f.get("token", [""])[0] != token:
                self._send(400, "bad token"); return
            if f.get("hc", [""])[0] != "true":
                self._send(400, "A real mouse click is required."); return
            v = f.get("verdict", [""])[0]
            if v not in ("approve", "reject"):
                self._send(400, "bad verdict"); return
            state["verdict"] = v
            try:
                ev = int(f.get("ev", ["0"])[0] or 0)
            except ValueError:
                ev = 0
            state["evidence"] = {"mouse_move_events": ev, "mouse_down": True}
            self._send(200, "Recorded.")

    try:
        server = socketserver.TCPServer(("127.0.0.1", port), H, bind_and_activate=True)
    except OSError:
        return None, "", token, state
    return server, f"http://127.0.0.1:{port}/p", token, state


def authorize(changed: list[dict]) -> tuple[str, dict]:
    """The authorization popup -- the git gate's mechanism. Shows each changed
    object whole in a scrollable, word-wrapped box, and blocks on a provably
    human decision (mouse movement, not a synthesisable click)."""
    server, url, token, state = _build_auth_server(changed)
    if server is None:
        return "timeout", {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.stderr.write(f"\nAuthorize agile backlog change at: {url}\n"); sys.stderr.flush()
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + AUTH_WINDOW_SECONDS
    while time.time() < deadline and state["verdict"] is None:
        time.sleep(0.25)
    server.shutdown()
    return (state["verdict"] or "timeout"), state["evidence"]


def broke_the_rules(reason: str, title: str = "Claude broke the rules") -> None:
    """The loud notice -- the same browser mechanism the git gate uses, one OK
    button that closes the window. Never silent."""
    rule_violation_popup.show(reason, title=title)


# ── the audit ────────────────────────────────────────────────────────────────
def commit_auth(changed: list[dict], evidence: dict) -> str:
    """Store the authorization with the new content, before any write. Returns
    the auth id so a later QA failure can roll this authorization back."""
    auth_id = secrets.token_hex(8)
    authorization_record.append({
        "kind": "agile_backlog_authorization",
        "auth_id": auth_id,
        "verdict": "approve",
        "changed_objects": changed,
        "human_evidence": evidence,
        "requirement": REQUIREMENT,
        "component": COMPONENT,
    }, tolerate_failure=False)
    return auth_id


def rollback_auth(auth_id: str, reason: str) -> None:
    """Record that an authorization was rolled back because its write did not
    stand. Append-only: the compensating entry, not a mutation."""
    try:
        authorization_record.append({
            "kind": "agile_backlog_authorization_rollback",
            "auth_id": auth_id,
            "verdict": "rolled_back",
            "reason": reason,
            "requirement": REQUIREMENT,
            "component": COMPONENT,
        }, tolerate_failure=False)
    except Exception as exc:  # noqa: BLE001
        log.error("auth rollback not recorded for %s: %s", auth_id, exc,
                  exc=ChatHealthyException(mode="audit_unrecorded",
                      component=COMPONENT,
                      message=f"auth rollback not recorded: {exc}",
                      exception=exc), if_not_debug_log=True)


def is_elevated() -> bool:
    """True when this process has the Administrators token enabled (Windows).

    Not the guard -- the guard is the OS refusing the write. Used to gate the
    --apply-write leaf so it only lands a write when it is actually elevated.
    """
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_elevated_copy(which: str) -> tuple[bool, bool, str]:
    """Land a cached file on the backlog through a one-shot UAC-elevated
    re-launch of this same program (--apply-write). The operator sees the UAC
    prompt only AFTER approving the change -- never before. `which` is "new"
    (the composed, authorized content) or "old" (rollback). Returns
    (ok, declined, reason); declined is True only when the operator refused the
    UAC prompt, so the caller can report that distinctly."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # noqa: BLE001
        return False, False, f"elevation support unavailable: {exc}"
    if not hasattr(ctypes, "windll"):
        return False, False, "elevation is a Windows-only path"

    # ShellExecuteExW with the "runas" verb is the only way to raise a UAC
    # prompt for a child; SEE_MASK_NOCLOSEPROCESS keeps the child handle so we
    # can wait on it and read its exit code.
    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x00000040
    sw_hide = 0
    error_cancelled = 1223
    infinite = 0xFFFFFFFF

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    # Only "new"/"old" ever reach the elevated leaf -- it maps them to the two
    # program-controlled cache files, so it can never be coerced into copying
    # an arbitrary file over the backlog.
    info.lpParameters = f'"{_HERE}" --apply-write {which}'
    info.nShow = sw_hide

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.windll.kernel32.GetLastError()
        if err == error_cancelled:
            return False, True, "operator declined the elevation prompt"
        return False, False, f"could not launch the elevated writer (WinError {err})"
    if not info.hProcess:
        return False, False, "elevated writer did not start"
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, infinite)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    if code.value != 0:
        return False, False, f"the elevated writer failed (exit {code.value})"
    return True, False, ""


def _land(which: str) -> tuple[bool, bool, str]:
    """Land one of the program's cached files onto the admin-only backlog.
    `which` is "new" (the composed, authorized content) or "old" (rollback).
    The direct copy is tried first; on an OS refusal -- this session is not
    elevated -- the write is retried via UAC. Returns (ok, declined, reason)."""
    src = CACHE_NEW if which == "new" else CACHE_OLD
    try:
        shutil.copyfile(src, TARGET)
        return True, False, ""
    except PermissionError:
        return _run_elevated_copy(which)


def _apply_write(which: str) -> int:
    """The elevated leaf: copy a cached file over the backlog. Reached only
    through _run_elevated_copy's "runas" re-launch of this program, never as an
    operator entry point. It refuses unless actually elevated -- a non-elevated
    invocation would be stopped by the ACL anyway, but refusing keeps this from
    ever being a quiet authorization bypass."""
    if not is_elevated():
        sys.stderr.write("--apply-write must run elevated.\n")
        return 3
    src = CACHE_NEW if which == "new" else CACHE_OLD
    try:
        shutil.copyfile(src, TARGET)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"elevated write failed: {exc}\n")
        return 1
    return 0


def _cleanup() -> None:
    CACHE_OLD.unlink(missing_ok=True)
    CACHE_NEW.unlink(missing_ok=True)


def run(objects_path: Path) -> int:
    path = objects_path.resolve()
    if not path.is_file():
        sys.stderr.write(f"objects file not found: {path}\n"); return 2
    try:
        loaded = _load(path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"objects file is not valid JSON: {exc}\n"); return 2
    # Accept a bare JSON array, or an object carrying the array under "objects"
    # (so the file can also hold a $schema and be a committable JSON).
    objects = loaded.get("objects") if isinstance(loaded, dict) else loaded
    if not isinstance(objects, list) or not objects:
        sys.stderr.write("objects file must provide a non-empty list of full "
                         "objects (a JSON array, or an object with an 'objects' "
                         "array).\n"); return 2

    # A -> B: compose the new file from the full objects, validate it.
    current = _load(TARGET)
    try:
        new = compose(current, objects)
        validate(new)
    except ChatHealthyException as exc:
        sys.stderr.write(f"cannot splice these objects: {exc}\n"); return 2
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"composed backlog is invalid (not written): {exc}\n")
        return 2

    changed = changed_objects(current, new)
    if not changed:
        sys.stderr.write("these objects change nothing; nothing to authorize.\n")
        return 0

    # B: cache the old file and the composed new file.
    CACHE_DIR.mkdir(exist_ok=True)
    shutil.copyfile(TARGET, CACHE_OLD)
    CACHE_NEW.write_text(_dumps(new), encoding="utf-8")

    # C: authorize.
    verdict, evidence = authorize(changed)
    if verdict != "approve":
        reason = ("operator rejected" if verdict == "reject"
                  else f"no decision within {AUTH_WINDOW_SECONDS}s")
        sys.stderr.write(f"authorization {verdict}: {reason}; nothing changed.\n")
        _cleanup(); return 1

    # C: store the authorization with the new content, before the write.
    try:
        auth_id = commit_auth(changed, evidence)
    except Exception as exc:  # noqa: BLE001
        log.error("auth not recorded: %s", exc,
                  exc=ChatHealthyException(mode="audit_unrecorded",
                      component=COMPONENT, message=f"auth not recorded: {exc}",
                      exception=exc), if_not_debug_log=True)
        sys.stderr.write("authorization could not be recorded; nothing changed.\n")
        _cleanup(); return 1

    # D: land the composed new file on the backlog. The write is attempted only
    # now, after authorization and the audit record. This session is normally
    # not elevated, so the direct copy is refused and the write is retried
    # through a UAC-elevated re-launch -- the operator elevates AFTER approving.
    landed, declined, reason = _land("new")
    if not landed:
        detail = reason or "the OS refused the write"
        broke_the_rules(
            f"The authorized change could not be written to the backlog: "
            f"{detail}. The authorization was rolled back; nothing changed.",
            title=("Elevation declined -- backlog unchanged" if declined
                   else "Backlog write failed -- unchanged"))
        sys.stderr.write(f"write not landed: {detail}; nothing changed.\n")
        rollback_auth(auth_id, f"write not landed: {detail}")
        _cleanup(); return 1

    # E: QA the actual written file.
    ok, why = qa(TARGET, new)
    if ok:
        _cleanup()
        log.info("agile backlog updated: %d object(s) changed (%s mouse events)",
                 len(changed), evidence.get("mouse_move_events", 0))
        return 0

    # F (fail): roll back to the cached old (via UAC again if the OS requires
    # it), notify, roll back the auth, retry.
    rolled_back, _rb_declined, rb_reason = _land("old")
    if not rolled_back:
        log.error("ROLLBACK FAILED, backlog may be inconsistent: %s", rb_reason,
                  exc=ChatHealthyException(mode="rollback_failed",
                      component=COMPONENT,
                      message=f"rollback write failed: {rb_reason}"),
                  if_not_debug_log=True)
    broke_the_rules(
        f"The write failed QA: {why}. "
        + ("Rolled back to the previous backlog; " if rolled_back
           else f"ROLLBACK ALSO FAILED ({rb_reason or 'unknown'}) -- the backlog "
                "may be inconsistent; ")
        + "the authorization was rolled back. Nothing stands; try again.",
        title="Backlog edit failed QA -- rolled back")
    rollback_auth(auth_id, f"failed QA: {why}")
    _cleanup()
    sys.stderr.write(f"QA failed: {why}; rolled back.\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agile_backlog_editor",
        description="The one sanctioned writer of the agile backlog.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--objects",
        help="Path to a JSON array of the full objects to splice "
             "(a file in _oneshots).")
    # Internal re-entry only -- the elevated leaf that lands a cached file on
    # the admin-only backlog. Not an operator entry point (hidden from --help).
    group.add_argument("--apply-write", choices=("new", "old"),
        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.apply_write:
        return _apply_write(args.apply_write)
    return run(Path(args.objects))


if __name__ == "__main__":
    sys.exit(main())
