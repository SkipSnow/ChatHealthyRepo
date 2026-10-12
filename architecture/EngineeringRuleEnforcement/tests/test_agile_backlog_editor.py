# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Behavioural tests for the sanctioned backlog editor against
EPIC-008-F-002-S-013-REQ-B-002, using real artifacts: the live backlog (read
only), the real schema, and the real OS ACL. No mocks of the data, no fake
Mongo.

The elevated path (approve -> write -> QA) needs admin rights and a human click
on the authorization page, so it is the operator's live test; these tests cover
everything observable without elevation and skip the rest.
"""
import copy
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE))

import agile_backlog_editor as ed              # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

REQ = "EPIC-008-F-002-S-013-REQ-B-002"


def _get(url):
    return urllib.request.urlopen(url, timeout=5).read().decode("utf-8")


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    return urllib.request.urlopen(urllib.request.Request(url, method="POST",
                                                         data=data), timeout=5)


@pytest.fixture(scope="module")
def current():
    return ed._load(ed.TARGET)


@pytest.fixture(scope="module")
def sample_req(current):
    return copy.deepcopy(ed._index_objects(current)[REQ])


def test_resplice_unchanged_is_a_noop(current, sample_req):
    assert ed.changed_objects(current, ed.compose(current, [sample_req])) == []


def test_modified_requirement_changes_exactly_that_object(current, sample_req):
    mod = copy.deepcopy(sample_req)
    mod["name"] = mod["name"] + " (edited)"
    new = ed.compose(current, [mod])
    changed = ed.changed_objects(current, new)
    assert [c["id"] for c in changed] == [REQ]
    ed.validate(new)


def test_unplaceable_object_raises(current):
    with pytest.raises(ChatHealthyException):
        ed.compose(current, [{"req_id": "EPIC-999-F-001-S-001-REQ-B-001"}])


def test_qa_passes_on_authorized_content(tmp_path, current, sample_req):
    mod = copy.deepcopy(sample_req)
    mod["name"] += " x"
    new = ed.compose(current, [mod])
    written = tmp_path / "written.json"
    written.write_text(ed._dumps(new), encoding="utf-8")
    assert ed.qa(written, new) == (True, "")


def test_qa_fails_on_corrupt_or_mismatched(tmp_path, current, sample_req):
    mod = copy.deepcopy(sample_req)
    mod["name"] += " y"
    new = ed.compose(current, [mod])
    written = tmp_path / "written.json"
    written.write_text("{ not valid json", encoding="utf-8")
    assert ed.qa(written, new)[0] is False
    written.write_text(ed._dumps(current), encoding="utf-8")
    assert ed.qa(written, new)[0] is False


def test_authorization_popup_shows_changes_and_requires_a_human(
        current, sample_req):
    mod = copy.deepcopy(sample_req)
    mod["name"] += " (popup test)"
    changed = ed.changed_objects(current, ed.compose(current, [mod]))
    server, url, token, state = ed._build_auth_server(changed)
    assert server is not None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = _get(url)
        assert REQ in page, "the changed object must be shown in the popup"
        assert "APPROVE" in page and "REJECT" in page

        # A click with no mouse evidence is refused, and no verdict is recorded.
        with pytest.raises(urllib.error.HTTPError) as err:
            _post_form(url.replace("/p", "/d"),
                       {"token": token, "verdict": "approve",
                        "hc": "false", "ev": "0"})
        assert err.value.code == 400
        assert state["verdict"] is None

        # A human decision (mouse evidence present) is accepted.
        _post_form(url.replace("/p", "/d"),
                   {"token": token, "verdict": "approve",
                    "hc": "true", "ev": "57"})
        for _ in range(40):
            if state["verdict"]:
                break
            time.sleep(0.05)
        assert state["verdict"] == "approve"
        assert state["evidence"]["mouse_move_events"] == 57
    finally:
        server.shutdown()


@pytest.mark.skipif(ed.is_elevated(),
                    reason="an elevated process can write by design")
def test_os_acl_blocks_a_non_elevated_write():
    with pytest.raises(PermissionError):
        open(ed.TARGET, "a", encoding="utf-8").close()


@pytest.mark.skipif(ed.is_elevated(),
                    reason="elevated runs proceed to authorization")
def test_non_elevated_run_bails_before_auth_and_writes_nothing(
        tmp_path, monkeypatch, current, sample_req):
    calls = []
    monkeypatch.setattr(ed, "broke_the_rules", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(ed, "authorize", lambda *a, **k: pytest.fail(
        "authorization must not be reached when not elevated"))
    mod = copy.deepcopy(sample_req)
    mod["name"] += " z"
    objects = tmp_path / "objects.json"
    objects.write_text(json.dumps([mod]), encoding="utf-8")

    before = ed.TARGET.read_bytes()
    assert ed.run(objects) == 1
    assert calls, "the non-elevated run must pop the loud notice"
    assert ed.TARGET.read_bytes() == before, "the backlog must be untouched"
    assert not ed.CACHE_OLD.exists() and not ed.CACHE_NEW.exists()
