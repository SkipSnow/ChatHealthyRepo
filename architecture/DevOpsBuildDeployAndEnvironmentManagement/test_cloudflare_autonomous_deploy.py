"""Autonomous per-package Cloudflare deploy — proof of EPIC-008-F-012-S-004.

These tests prove the fix for the 2026-09-23 whole-site-wipe: a website
deploy of one package MUST publish the complete current site with only that
package's files replaced, so it disturbs no other package
(EPIC-008-F-012-S-004-REQ-B-002). Cloudflare Pages deployments are
whole-site atomic, so autonomy is achieved by carrying every package this
run is NOT installing forward, byte-for-byte, from the live deployment; a
carried package that is not live aborts the deploy rather than publishing a
site that drops it (REQ-B-011: whole or not at all).

The Cloudflare enumeration and byte-fetch are monkeypatched so the proof
runs with no network and no real build. The manifest read is the real,
edited deployment_architecture.json (copied into a temp repo root), so the
`Data` package split and the env-scoped local_host are exercised as shipped.

TEST-PLAN LINKAGE (operator to author): EPIC-008-F-012-S-004-REQ-B-002 names
test file architecture/EngineeringRuleEnforcement/tests/test_deploy_authorization.py,
which does not exist. This file proves the requirement and needs a test_id
minted and linked in agile_backlog.json by the operator; authoring that
linkage is not Claude's.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _deploy_chain  # noqa: E402
from _build_chain import _declared_packages  # noqa: E402
from record_loader import RecordLoader  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

WEBSITE = _deploy_chain.WEBSITE_TARGET_ID
_REPO = _HERE.parents[1]
_MANIFEST_REL = Path("brain/machine_artifacts/content/deployment_architecture.json")


class _FakeResolver:
    def resolve(self, name: str, env: str) -> str:
        return f"fake-{name}-{env}"


def _temp_repo(tmp_path: Path) -> Path:
    """A repo root holding the real, edited manifest and an empty build tree."""
    dst = tmp_path / _MANIFEST_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_REPO / _MANIFEST_REL, dst)
    return tmp_path


def _target(repo: Path):
    coll = RecordLoader().load_collection(repo / _MANIFEST_REL)
    return coll.by_target_id(WEBSITE)


def _declared_served(target, pid: str) -> list[str]:
    return [_deploy_chain._served_path(f.source_location)
            for f in target.files
            if f.package == pid
            and _deploy_chain._served_path(f.source_location) is not None]


def _stage(repo: Path, pid: str, served_to_bytes: dict[str, bytes]) -> None:
    root = repo / "build" / WEBSITE / pid / "Website"
    for served, data in served_to_bytes.items():
        p = root / served.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _patch_live(monkeypatch, dep_url: str, live: dict[str, bytes]) -> None:
    monkeypatch.setattr(
        _deploy_chain, "_cf_live_deployment_files",
        lambda account, project, token: (dep_url, {k: "h" for k in live}))
    monkeypatch.setattr(
        _deploy_chain, "_cf_fetch_live_bytes",
        lambda url, path: live[path])


def _read_out(out: Path) -> dict[str, bytes]:
    return {"/" + p.relative_to(out).as_posix(): p.read_bytes()
            for p in out.rglob("*") if p.is_file()}


# ── model: Data is its own package; local server is env-scoped ──────────────

def test_data_is_its_own_content_package(tmp_path):
    repo = _temp_repo(tmp_path)
    content = _deploy_chain._content_packages(repo, "dev")
    assert "Data" in content
    target = _target(repo)
    data_files = [f.source_location for f in target.files if f.package == "Data"]
    assert data_files == ["Website/Data/specialty_classification_gpt41.json"]
    # no Data/* file is left claimed by another package (read-back clean merge)
    others = [f.source_location for f in target.files
              if f.package != "Data" and f.source_location.startswith("Website/Data/")]
    assert others == []


def test_local_host_is_local_only_not_in_cloud_set(tmp_path):
    target = _target(_temp_repo(tmp_path))
    for env in ("dev", "qa", "prod"):
        pkgs = _declared_packages(target, env)
        assert "local_host" not in pkgs, f"local_host leaked into {env}"
        assert "Data" in pkgs
    assert "local_host" in _declared_packages(target, "local")


# ── autonomy: a one-package deploy preserves every other package ────────────

def test_json_schemas_only_deploy_preserves_other_packages(tmp_path, monkeypatch):
    repo = _temp_repo(tmp_path)
    target = _target(repo)

    # A complete live site: every declared content path + generated files.
    live: dict[str, bytes] = {}
    for pid in _deploy_chain._content_packages(repo, "dev"):
        for sp in _declared_served(target, pid):
            live[sp] = f"LIVE {sp}".encode()
    live["/app/index.html"] = b"LIVE app index"          # generated bundle
    live["/app/assets/main.abc123.js"] = b"LIVE bundle"  # generated, undeclared
    live["/build.js"] = b"LIVE build provenance"         # undeclared root file
    _patch_live(monkeypatch, "https://dep.example.pages.dev", live)

    # Build only json_schemas, with FRESH bytes.
    fresh = {sp: f"FRESH {sp}".encode() for sp in _declared_served(target, "json_schemas")}
    _stage(repo, "json_schemas", fresh)

    out = _deploy_chain._website_autonomous_publish_dir(
        repo, target, "dev", {"json_schemas"}, _FakeResolver())
    published = _read_out(out)

    # Every live path is still present — nothing dropped.
    for sp in live:
        assert sp in published, f"{sp} was dropped by a json_schemas-only deploy"
    # json_schemas came from the fresh build.
    for sp, data in fresh.items():
        assert published[sp] == data
    # Every OTHER package (and generated files) is byte-for-byte the live copy.
    for sp, data in live.items():
        if sp in fresh:
            continue
        assert published[sp] == data, f"{sp} was disturbed"


def test_data_only_deploy_preserves_other_packages(tmp_path, monkeypatch):
    repo = _temp_repo(tmp_path)
    target = _target(repo)
    live: dict[str, bytes] = {}
    for pid in _deploy_chain._content_packages(repo, "dev"):
        for sp in _declared_served(target, pid):
            live[sp] = f"LIVE {sp}".encode()
    live["/app/index.html"] = b"LIVE app"
    _patch_live(monkeypatch, "https://dep.example.pages.dev", live)

    fresh = {sp: f"FRESH DATA {sp}".encode() for sp in _declared_served(target, "Data")}
    _stage(repo, "Data", fresh)

    out = _deploy_chain._website_autonomous_publish_dir(
        repo, target, "dev", {"Data"}, _FakeResolver())
    published = _read_out(out)

    for sp in live:
        assert sp in published
    for sp, data in fresh.items():
        assert published[sp] == data           # Data updated from build
    for sp, data in live.items():
        if sp not in fresh:
            assert published[sp] == data        # everything else preserved


# ── safety: the exact 2026-09-23 failure mode now ABORTS, never publishes ────

def test_partial_deploy_aborts_when_a_carried_package_is_not_live(tmp_path, monkeypatch):
    repo = _temp_repo(tmp_path)
    target = _target(repo)

    # Live site is WIPED of static_pages and runtime_driver (the dev state
    # after 2026-09-23). json_schemas is still live.
    live: dict[str, bytes] = {}
    for sp in _declared_served(target, "json_schemas"):
        live[sp] = b"LIVE schema"
    _patch_live(monkeypatch, "https://dep.example.pages.dev", live)

    _stage(repo, "json_schemas",
           {sp: b"FRESH" for sp in _declared_served(target, "json_schemas")})

    with pytest.raises(ChatHealthyException) as exc:
        _deploy_chain._website_autonomous_publish_dir(
            repo, target, "dev", {"json_schemas"}, _FakeResolver())
    msg = str(exc.value)
    assert "not fully present on the live site" in msg
    # Nothing was published: no partial site left behind.
    assert not (repo / "build" / WEBSITE / "_publish").exists()


# ── recovery: a full deploy publishes the complete site from the build ───────

def test_full_deploy_publishes_complete_site_with_no_live(tmp_path, monkeypatch):
    repo = _temp_repo(tmp_path)
    target = _target(repo)
    # No prior deployment at all (project never fully deployed / wiped).
    _patch_live(monkeypatch, "", {})

    all_content = _deploy_chain._content_packages(repo, "dev")
    expected: dict[str, bytes] = {}
    for pid in all_content:
        staged = {sp: f"BUILD {pid} {sp}".encode() for sp in _declared_served(target, pid)}
        _stage(repo, pid, staged)
        expected.update(staged)

    # selection None == "install every declared package" (full deploy).
    out = _deploy_chain._website_autonomous_publish_dir(
        repo, target, "dev", None, _FakeResolver())
    published = _read_out(out)
    assert published == expected
