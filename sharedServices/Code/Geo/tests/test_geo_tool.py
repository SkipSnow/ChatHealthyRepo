"""Unit tests for the Geo tool.

The extractor (an LLM) and UserParameters (the page-scoped store) are both
stubbed, so the test proves the tool's own logic: it extracts, writes the
located parts onto the page it was told to service, and returns them.
"""
import asyncio
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ChatHealthyLib" / "src"))
sys.path.insert(0, str(ROOT / "sharedServices" / "Code"))

# A fake UserParameters that captures the write instead of touching a DB.
_captured: dict = {}


class _Change:
    def __init__(self, page, name, value):
        self.page, self.name, self.value = page, name, value


class _Request:
    def __init__(self, **kw):
        self.kw = kw


class _Tool:
    async def run(self, deps, request):
        _captured["request"] = request


_fake = types.ModuleType("UserParameters")
_upt = types.ModuleType("UserParameters.user_parameters_tool")
_upt.Change = _Change
_upt.Request = _Request
_upt.TOOL = _Tool()
_fake.user_parameters_tool = _upt
sys.modules.setdefault("UserParameters", _fake)
sys.modules.setdefault("UserParameters.user_parameters_tool", _upt)

from Geo import geo_tool  # noqa: E402
from chathealthy_lib import geo_extractor  # noqa: E402


def test_geo_extracts_and_writes_the_named_page(monkeypatch):
    _captured.clear()
    monkeypatch.setattr(
        geo_extractor, "extract_location",
        lambda text: geo_extractor.LocationOutput(state="NY", city="Brooklyn"))

    resp = asyncio.run(geo_tool.TOOL.run(
        object(),
        geo_tool.Request(page="individualProvider",
                         utterance="a foot doctor in Brooklyn NY")))

    assert resp.page == "individualProvider"
    assert resp.state == "NY"
    assert resp.city == "Brooklyn"

    req = _captured["request"]
    assert {c.page for c in req.kw["changes"]} == {"individualProvider"}
    assert {"state", "city"} <= {c.name for c in req.kw["changes"]}


def test_geo_services_the_facility_page_when_told(monkeypatch):
    _captured.clear()
    monkeypatch.setattr(
        geo_extractor, "extract_location",
        lambda text: geo_extractor.LocationOutput(state="CA"))

    resp = asyncio.run(geo_tool.TOOL.run(
        object(),
        geo_tool.Request(page="facility", utterance="a hospital in California")))

    assert resp.page == "facility"
    assert {c.page for c in _captured["request"].kw["changes"]} == {"facility"}


def test_geo_requires_a_page():
    from chathealthy_lib.exceptions import ChatHealthyException
    with pytest.raises(ChatHealthyException):
        asyncio.run(geo_tool.TOOL.run(
            object(), geo_tool.Request(page="", utterance="anywhere")))


def test_geo_empty_utterance_writes_nothing(monkeypatch):
    _captured.clear()
    resp = asyncio.run(geo_tool.TOOL.run(
        object(), geo_tool.Request(page="individualProvider", utterance="   ")))
    assert resp.page == "individualProvider"
    assert "request" not in _captured
