"""Binds the chain's Azure calls to a named identity.

Every Azure call in the build, deploy and promote chain goes out through the
`az` CLI as a subprocess, and `az` resolves who it is from the login cached in
AZURE_CONFIG_DIR. With that variable unset it reads ~/.azure, so the chain
acts as whoever last typed `az login` at the terminal. That made the acting
identity a property of the workstation rather than of the code.

This module sets AZURE_CONFIG_DIR to a private directory and logs into it as
the named identity, so every `az` subprocess the chain spawns inherits that
identity and no other. The operator's own ~/.azure login is neither read nor
modified, and the private directory is removed when the process exits, so no
credential is cached between runs.

The deploy acts as DevOpsUser. Nothing in the chain acts as the interactive
CLI identity.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _ch_d in Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in sys.path:
            sys.path.insert(0, str(_ch_lib))
        break

import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_CH_LOG = ChatHealthyLoggingService()

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run_az(args: list[str]) -> subprocess.CompletedProcess:
    """`az` is a .cmd on Windows, which CreateProcess will not resolve; the
    rest of the chain shells out for the same reason."""
    return subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_NO_WINDOW, shell=(sys.platform == "win32"),
    )


def _chathealthy_exception():
    """ChatHealthyException, imported without assuming the library is on the
    path. These scripts run as bare entry points before any package setup."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for p in (cur, *cur.parents):
        if (p / ".git").is_dir() or (p / ".env").is_file():
            return p
    raise ChatHealthyException(
        mode="repo_root_not_found",
        component="DevOpsIdentity",
        message=f"repo root not found walking up from {Path(__file__).resolve()}")


def _credential(identity: str) -> tuple[str, str, str]:
    """The three .env values for one identity.

    Read straight out of the file rather than through load_dotenv, because the
    chain deliberately pins CH_LOG_DESTINATION and an override-load of the
    application .env would undo it.
    """
    from dotenv import dotenv_values

    prefix = identity.upper()
    values = dotenv_values(_repo_root() / ".env")
    keys = (f"{prefix}_AZURE_CLIENT_ID",
            f"{prefix}_AZURE_CLIENT_SECRET",
            f"{prefix}_AZURE_TENANT_ID")
    missing = [k for k in keys if not values.get(k)]
    if missing:
        raise ChatHealthyException(
            mode="azure_credential_missing",
            component="DevOpsIdentity",
            message=f"cannot act as {identity}: .env is missing {', '.join(missing)}",
            context={"identity": identity, "missing": missing})
    return tuple(values[k] for k in keys)          # type: ignore[return-value]


def establish_azure_identity(identity: str) -> str:
    """Log the chain's Azure surface in as `identity` and return its client id.

    Sets AZURE_CONFIG_DIR for this process, so every `az` subprocess spawned
    afterwards — at any call site, in any module — authenticates as this
    identity regardless of the workstation's own login state.
    """
    client_id, secret, tenant = _credential(identity)

    config_dir = tempfile.mkdtemp(prefix=f"azcfg-{identity}-")
    atexit.register(shutil.rmtree, config_dir, True)
    os.environ["AZURE_CONFIG_DIR"] = config_dir

    # A private config dir isolates installed CLI extensions along with the
    # login, and the chain needs several of them -- automation, containerapp.
    # Point the extension lookup back at the workstation's own directory: the
    # extensions are code the CLI runs, not a credential, so sharing them
    # isolates nothing that matters.
    if not os.environ.get("AZURE_EXTENSION_DIR"):
        installed = Path.home() / ".azure" / "cliextensions"
        if installed.is_dir():
            os.environ["AZURE_EXTENSION_DIR"] = str(installed)

    login = _run_az(
        ["az", "login", "--service-principal",
         "--username", client_id, "--password", secret, "--tenant", tenant,
         "--allow-no-subscriptions", "-o", "none"])
    if login.returncode != 0:
        raise ChatHealthyException(
            mode="azure_login_failed",
            component="DevOpsIdentity",
            message=f"az login as {identity} ({client_id}) failed: "
                    f"{login.stderr.strip()[:400]}",
            context={"identity": identity, "client_id": client_id})

    shown = _run_az(
        ["az", "account", "show", "--query", "user.name", "-o", "tsv"])
    acting = shown.stdout.strip()
    if acting != client_id:
        raise ChatHealthyException(
            mode="azure_identity_mismatch",
            component="DevOpsIdentity",
            message=f"expected the chain to act as {identity} ({client_id}) "
                    f"but az reports {acting or 'no account'}",
            context={"identity": identity,
                     "expected": client_id, "acting": acting})

    return client_id
