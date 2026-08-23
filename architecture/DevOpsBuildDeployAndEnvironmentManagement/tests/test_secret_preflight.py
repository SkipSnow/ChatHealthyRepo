"""What the build asks about a secret, and what the deploy asks.

Every case here is one the chain got wrong or could get wrong silently. The
two that matter most are the ones about absence: a vault that refuses a read
and a vault that does not hold the secret look identical from the outside,
and confusing them means overwriting a live credential with whatever the
workstation happens to hold.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

for _d in _HERE.parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in sys.path:
            sys.path.insert(0, str(_lib))
        break

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

import secret_preflight  # noqa: E402
from secrets_resolver import SecretsResolver, vault_secret_name  # noqa: E402


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def env_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / ".env"
    path.write_text(
        "# Secrets\n"
        "OPENAI_API_KEY=sk-local\n"
        "API_TOKEN_MAP={\"tok\":\"caller\"}\n"
        "# SecretSafe\n"
        "ENV_PREFIX=dev\n",
        encoding="utf-8")
    return path


def _resolver(env_file: pathlib.Path, **bindings) -> SecretsResolver:
    return SecretsResolver(
        bindings={(name, "dev"): store for name, store in bindings.items()},
        env_file=env_file,
        vaults={"dev": "kv-test-dev"})


# ── the vault name carries no deployment detail ──────────────────────────

def test_vault_name_is_the_secret_name_with_hyphens():
    assert vault_secret_name("OPENAI_API_KEY") == "OPENAI-API-KEY"


@pytest.mark.parametrize("name", ["API_TOKEN_MAP", "HF_TOKEN"])
def test_vault_name_carries_no_environment_target_or_package(name):
    produced = vault_secret_name(name)
    for leak in ("dev", "qa", "prod", "target_", "service_runtime"):
        assert leak not in produced


# ── an environment reads its own vault, or none at all ───────────────────

def test_an_environment_with_no_declared_vault_does_not_borrow_another(env_file):
    resolver = SecretsResolver(bindings={("OPENAI_API_KEY", "prod"): "azure_key_vault"},
                               env_file=env_file, vaults={"dev": "kv-test-dev"})
    with pytest.raises(ChatHealthyException) as caught:
        resolver.vault_for("prod")
    assert "declares no Key Vault" in caught.value.message


# ── absence vs refusal: the distinction the seeding depends on ───────────

def test_a_missing_secret_reads_as_absent(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="ERROR: (SecretNotFound) A secret with (name/id) X was not found"))
    value, absent = SecretsResolver._vault_read("kv-test-dev", "X")
    assert absent is True
    assert value == ""


def test_a_refused_read_is_not_absence_and_raises(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="ERROR: (Forbidden) Caller is not authorized to perform action"))
    with pytest.raises(ChatHealthyException) as caught:
        SecretsResolver._vault_read("kv-test-dev", "X")
    assert "not the vault reporting it absent" in caught.value.message


def test_a_refused_read_never_seeds(monkeypatch, env_file):
    """The whole point: a secret we merely cannot read is not overwritten."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="ERROR: (Forbidden) Caller is not authorized"))
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    with pytest.raises(ChatHealthyException):
        resolver.resolve("OPENAI_API_KEY", "dev")
    assert resolver.seeded() == []


def test_an_absent_secret_is_seeded_from_env_once(monkeypatch, env_file):
    writes: list[tuple[str, str, str]] = []

    def fake_run(argv, *a, **k):
        # az keyvault secret show|set --vault-name V --name N [--value X]
        if argv[3] == "show":
            return _Completed(1, stderr="(SecretNotFound) not found")
        writes.append((argv[5], argv[7], argv[9]))
        return _Completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    assert resolver.resolve("OPENAI_API_KEY", "dev") == "sk-local"
    assert writes == [("kv-test-dev", "OPENAI-API-KEY", "sk-local")]
    assert resolver.seeded() == [("kv-test-dev", "OPENAI-API-KEY")]
    resolver.resolve("OPENAI_API_KEY", "dev")
    assert len(writes) == 1, "a cached value must not be written a second time"


def test_a_secret_absent_from_both_vault_and_env_is_reported_not_invented(
        monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="(SecretNotFound) not found"))
    resolver = _resolver(env_file, NOWHERE_AT_ALL="azure_key_vault")
    with pytest.raises(ChatHealthyException) as caught:
        resolver.resolve("NOWHERE_AT_ALL", "dev")
    assert "exists" in caught.value.message and "nowhere" in caught.value.message


# ── the build's question: presence, never a value ────────────────────────

def test_the_build_confirms_presence_without_returning_a_value(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(0, stdout="sk-live\n"))
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    present, detail = resolver.exists("OPENAI_API_KEY", "dev")
    assert present is True
    assert "sk-live" not in detail and "sk-local" not in detail


def test_the_build_accepts_a_secret_the_deploy_will_seed(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="(SecretNotFound) not found"))
    resolver = _resolver(env_file, API_TOKEN_MAP="azure_key_vault")
    present, detail = resolver.exists("API_TOKEN_MAP", "dev")
    assert present is True
    assert "seed" in detail


def test_the_build_refuses_a_secret_that_exists_nowhere(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(
        1, stderr="(SecretNotFound) not found"))
    resolver = _resolver(env_file, ABSENT_EVERYWHERE="azure_key_vault")
    present, _ = resolver.exists("ABSENT_EVERYWHERE", "dev")
    assert present is False


# ── the deploy's question: the two copies agree ──────────────────────────

def test_the_deploy_passes_when_vault_and_env_hold_the_same_bytes(
        monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(0, stdout="sk-local"))
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    agrees, _ = resolver.verify_matches_local("OPENAI_API_KEY", "dev")
    assert agrees is True


def test_the_deploy_stops_when_the_copies_differ_and_names_neither_correct(
        monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(0, stdout="sk-rotated"))
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    agrees, detail = resolver.verify_matches_local("OPENAI_API_KEY", "dev")
    assert agrees is False
    assert "Neither is assumed correct" in detail
    assert "sk-rotated" not in detail and "sk-local" not in detail


def test_the_mismatch_report_names_no_value(monkeypatch, env_file):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(0, stdout="sk-rotated"))
    resolver = _resolver(env_file, OPENAI_API_KEY="azure_key_vault")
    _, detail = resolver.verify_matches_local("OPENAI_API_KEY", "dev")
    assert len(detail.split("vault ")[1].split(",")[0]) == 12, "digest, not the value"


# ── deploy-computed qualifiers are not missing secrets ───────────────────

def test_a_rename_points_the_check_at_the_name_it_renames():
    assert secret_preflight._subject_of(
        "FINDCARE_SIGNING_KEY_PEM",
        "rename_from:FINDCARE_SIGNING_KEY_B64") == "FINDCARE_SIGNING_KEY_B64"


def test_a_plain_store_declaration_stands_for_itself():
    assert secret_preflight._subject_of("HF_TOKEN", "azure_key_vault") == "HF_TOKEN"


@pytest.mark.parametrize("qualifier", [
    "peer_url:target_hf_space_findcare_backend",
    "local_cert_file:Code/Shared/ops/certs/ca.crt",
])
def test_a_deploy_computed_value_has_nothing_to_confirm(qualifier):
    assert secret_preflight._subject_of("ANY_NAME", qualifier) is None
