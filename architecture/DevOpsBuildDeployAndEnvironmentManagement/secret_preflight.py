"""The two questions the chain asks about secrets before it acts.

A build asks whether every secret its targets declare can be produced. It
does not ask what any of them is: a build that read the values would carry
credentials into a build directory and from there into an image layer, and
the whole reason the vault exists is that credentials do not travel in
build output. So the build's question is presence, and its answer is a
name and a location, never a value.

A deploy asks something stricter, because by then the value is about to be
installed somewhere: does the vault's copy and the workstation's fair copy
agree. The fair copy is maintained by hand and nothing has ever compared
the two, so they are expected to be identical and have never been checked.

Both questions are asked here so that the build and the deploy cannot
answer them differently.

Imported-only. No entry point.
"""
from __future__ import annotations

from pathlib import Path

import sys as _ch_sys
import pathlib as _ch_pl

for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

from secrets_resolver import SecretsResolver  # noqa: E402

_log = ChatHealthyLoggingService()


def declared_secrets(coll, env: str,
                     target_ids: list[str] | None = None,
                     packages: set[str] | None = None) -> list[tuple[str, str, str]]:
    """Every (target_id, package_id, secret_name) in scope for this run.

    Secrets are declared in two places and both are read. A target-level
    block applies to every package that target carries; a package-level
    block applies to that package alone. Package level is the one that says
    which capability actually needs the credential, and it is what lets a
    target hold two packages without handing each the other's secrets.
    """
    found: list[tuple[str, str, str]] = []
    for record in coll:
        if target_ids is not None and record.target_id not in target_ids:
            continue
        bindings = [b for b in record.environments if b.env_binding == env]
        if not bindings:
            continue
        declared_packages = []
        for binding in bindings:
            declared_packages.extend(binding.packages or [])
        for name, qualifier in (record.secrets or {}).items():
            for package in declared_packages:
                pid = package.get("package_id", "")
                if packages and pid not in packages:
                    continue
                found.append((record.target_id, pid, name, qualifier))
            if not declared_packages:
                found.append((record.target_id, "", name, qualifier))
        for package in declared_packages:
            pid = package.get("package_id", "")
            if packages and pid not in packages:
                continue
            for name, qualifier in (package.get("secrets") or {}).items():
                found.append((record.target_id, pid, name, qualifier))
    return found


_RENAME_FROM = "rename_from:"


def _subject_of(name: str, qualifier: str) -> str | None:
    """Which name actually has to exist for this declaration to be satisfiable.

    Most declarations name a store and stand for themselves. A few carry a
    source qualifier instead, meaning the value is computed when the deploy
    runs rather than read from anywhere:

      rename_from:<other>  the value of <other>, pushed under this name. The
                           thing that must exist is <other>, not this.

    Any other qualifier is deploy-computed with no antecedent -- a peer URL,
    the environment's own name, a certificate read off disk -- and there is
    nothing for a build to confirm the existence of. Returning None says so
    rather than reporting a credential missing that was never stored.
    """
    if qualifier.startswith(_RENAME_FROM):
        source = qualifier[len(_RENAME_FROM):].strip()
        return source or None
    if ":" in qualifier:
        return None
    return name


def _resolver(coll, repo_root: Path) -> SecretsResolver:
    return SecretsResolver.from_collection(coll, env_file=repo_root / ".env")


def confirm_secrets_exist(coll, env: str, repo_root: Path,
                          target_ids: list[str] | None = None,
                          packages: set[str] | None = None) -> None:
    """The build's question. Presence only; no value is read.

    Raises when anything in scope cannot be produced, because a build whose
    secrets do not exist is a build that cannot be deployed, and shipping it
    only moves the failure to the environment it was going to change.
    """
    resolver = _resolver(coll, repo_root)
    scope = declared_secrets(coll, env, target_ids, packages)
    if not scope:
        _log.info(f"[secrets] {env}: no secrets declared in scope")
        return
    missing: list[str] = []
    checked: set[tuple[str, str]] = set()
    for target_id, package_id, name, qualifier in scope:
        subject = _subject_of(name, qualifier)
        if subject is None or (subject, env) in checked:
            continue
        checked.add((subject, env))
        present, detail = resolver.exists(subject, env)
        if not present:
            missing.append(f"{target_id}/{package_id or '-'}: {subject} - {detail}")
    if missing:
        listed = "; ".join(missing)
        raise ChatHealthyException(
            mode="manifest_incomplete",
            component="secret_preflight",
            message=f"{len(missing)} declared secret(s) for env {env!r} exist "
                    f"nowhere they can be read from, so this build could not "
                    f"be deployed: {listed}")
    _log.info(f"[secrets] {env}: {len(checked)} declared secret(s) confirmed present")


def confirm_secrets_match_local(coll, env: str, repo_root: Path,
                                target_ids: list[str] | None = None,
                                packages: set[str] | None = None) -> None:
    """The deploy's question. The two copies agree, compared by digest.

    Raises on divergence without choosing a side. Which copy is right is a
    fact about what was rotated and when, and this code does not know it.
    """
    resolver = _resolver(coll, repo_root)
    scope = declared_secrets(coll, env, target_ids, packages)
    diverged: list[str] = []
    checked: set[tuple[str, str]] = set()
    for _target_id, _package_id, name, qualifier in scope:
        subject = _subject_of(name, qualifier)
        if subject is None or (subject, env) in checked:
            continue
        checked.add((subject, env))
        agrees, detail = resolver.verify_matches_local(subject, env)
        if not agrees:
            diverged.append(detail)
    if diverged:
        listed = "; ".join(diverged)
        raise ChatHealthyException(
            mode="security_violation",
            component="secret_preflight",
            message=f"{len(diverged)} secret(s) differ between the vault and "
                    f"the local .env for env {env!r}. The deploy stops rather "
                    f"than install either copy: {listed}")
    _log.info(f"[secrets] {env}: {len(checked)} secret(s) agree between vault and .env")
