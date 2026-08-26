# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical MongoClient provider for ChatHealthy.ai front-end services.

X.509 mTLS authentication only. No connection strings. No alternative auth paths.

Realizes EPIC-008-F-002-S-010. Direct MongoClient(...) instantiation in
any ChatHealthy.ai-authored Python file outside THIS file is forbidden
and enforced at pre-commit via Rule-004.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

from bson.json_util import dumps as bson_dumps
import httpx
from pymongo import MongoClient
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    DocumentTooLarge,
    EncryptionError,
    InvalidDocument,
    InvalidName,
    InvalidOperation,
    OperationFailure,
    ProtocolError,
    ServerSelectionTimeoutError,
)

from .exceptions import ChatHealthyException
from .logging_service import ChatHealthyLoggingService

log = ChatHealthyLoggingService()
TIMEOUT_MS = 120000

# A cluster is named by its Atlas cluster name and by nothing else. There is
# no alias, no logical target and no purpose-word: what a caller writes is
# what a reader can look up in Atlas.
# EPIC-008-F-002-S-010-REQ-B-010: this library holds zero Mongo cluster
# facts. It knew four of them -- which clusters exist, what each one's
# hostname is, how many times to retry each, and which one's client may be
# cached -- and every one of those is a deployment fact about ChatHealthy,
# living in a library that is meant to be about connecting to Mongo. The
# cluster name arrives as an opaque string and the library looks nothing up.
#
# A cluster maps to a HOST, never to a credentialed connection string: the
# credential is the identity's certificate, so no secret is involved in
# deciding where to connect. The host is TOLD to the library through
# CH_MONGO_HOST_<CLUSTER>, which the deploy sets per target. That variable
# already existed as an override for Azure compute reaching Atlas over
# Private Link; it is now the only source, so the public and private routes
# are the same mechanism rather than one being a hardcoded default and the
# other an exception.
#
# Retry and caching follow from what the caller knows about its cluster, not
# from a list here naming ChatHealthy's. Both have neutral defaults and an
# environment override: a cluster that pauses wants more attempts and no
# cached client, and the deployment that knows it pauses is what says so.
_CONNECT_BACKOFF_SECONDS = 5


# EPIC-008-F-002-S-010-REQ-B-010: no datum needed to reach a cluster lives
# in this file. Not the cluster names, not the hosts, not the retry counts,
# not which client may be cached. This library is told, and it holds nothing.
#
# Told by the VAULT. A collection would be the other home, and it is where
# public facts belong, but a collection cannot answer the question "where is
# the cluster" -- reading it requires the connection being made. The vault
# has no such problem: it is already the FIRST login, ahead of Mongo, because
# the certificate comes from there. So one more secret on that same call
# costs a round trip nobody was saving and closes the circularity.
#
# The host is not a secret -- it is a public DNS name, and the certificate is
# the credential -- but the vault holds it anyway, because being reachable
# before Mongo matters more here than the secret/non-secret distinction.
_HOST_CACHE: dict[str, str] = {}
_TAG_CACHE: dict[str, dict] = {}
# One live client per (identity, cluster). Not a cluster fact -- a
# process-local cache -- and it went out with the four that were.
# Keyed by identity, cluster AND version mode: the mode changes which
# collection a name resolves to, so two modes are two different handles.
_CLIENT_CACHE: dict[tuple[str, str, bool], "TimedClient"] = {}
_VAULT_HOST_PREFIX = "mongo-host-"
_VAULT_ATTEMPTS_PREFIX = "mongo-attempts-"
_VAULT_NO_CACHE_PREFIX = "mongo-nocache-"


def q(value: Any) -> str:
    """Render the verbatim JSON the application is asking pymongo to send."""
    try:
        return bson_dumps(value, default=str)
    except Exception:
        return repr(value)


def _exc_detail(exc: BaseException) -> str:
    """Render verbatim the Mongo-side response carried on a pymongo exception:
    .code, .code_name, and .details (the raw server reply document)."""
    parts = []
    code = getattr(exc, "code", None)
    if code is not None:
        parts.append(f"code={code}")
    code_name = getattr(exc, "code_name", None)
    if code_name is not None:
        parts.append(f"code_name={code_name}")
    details = getattr(exc, "details", None)
    if details is not None:
        parts.append(f"details={q(details)}")
    errors = getattr(exc, "errors", None)
    if errors:
        parts.append(f"errors={q(errors)}")
    return " ".join(parts)


def _classify_mongo_exception(exc: BaseException, elapsed_s: float) -> str:
    """Map a pymongo error to a ChatHealthy mode. Empty string for non-pymongo
    exceptions - caller re-raises raw.

    `mongo_query_timeout` ONLY fires when elapsed is within 2s of the
    configured client budget (TIMEOUT_MS). Anything else from the
    ConnectionFailure family is `mongo_network_failure` - the connection
    layer dropped for some reason that is NOT a budget exhaustion."""
    if isinstance(exc, (ConnectionFailure, ServerSelectionTimeoutError)):
        if elapsed_s >= (TIMEOUT_MS / 1000.0) - 2.0:
            return "mongo_query_timeout"
        return "mongo_network_failure"
    if isinstance(exc, DocumentTooLarge):
        return "mongo_document_too_large"
    if isinstance(exc, OperationFailure):
        return "mongo_server_rejected"
    if isinstance(exc, (ConfigurationError, InvalidOperation, InvalidName, InvalidDocument)):
        return "mongo_invalid_operation"
    if isinstance(exc, (ProtocolError, EncryptionError)):
        return "mongo_protocol_failure"
    return ""


def _convert_mongo_exception(exc: BaseException, elapsed_s: float,
                              op: str, db: str, coll: str) -> None:
    """Translate a recognised pymongo exception into a ChatHealthyException
    carrying the verbatim Mongo response in context, raised `from exc`. For
    an unrecognised exception, return None - the caller re-raises raw."""
    mode = _classify_mongo_exception(exc, elapsed_s)
    if not mode:
        return
    raise ChatHealthyException(
        mode=mode,
        message=f"mongo.{op} {db}.{coll} {type(exc).__name__}: {exc}",
        component="ChatHealthyMongoUtilities",
        elapsed_s=round(elapsed_s, 3),
        mongo_detail=_exc_detail(exc),
        op=op,
        db=db,
        coll=coll,
    ) from exc


class TimedCursor:
    """Pass-through cursor wrapper. Logs FAIL only.

    Per-operation START/END tracing was removed: it produced two records
    per database call in the one shared business log, drowning the events
    that mean something. Failures are rare and are the thing worth keeping.
    """

    def __init__(self, cursor: Any, op: str, db: str, coll: str) -> None:
        self._cursor = cursor
        self._op = op
        self._db = db
        self._coll = coll

    def __iter__(self):
        start = time.monotonic()
        try:
            for doc in self._cursor:
                yield doc
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.%s iter FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._op, self._db, self._coll, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            try:
                self._cursor.close()
            except Exception:
                pass
            _convert_mongo_exception(exc, elapsed, f"{self._op}_iter",
                                     self._db, self._coll)
            raise

    def close(self) -> None:
        """Explicit close - delegate to the wrapped pymongo cursor.
        Surfaced so callers can use `with closing(coll.find(...))` and
        guarantee the server-side cursor is released even when the
        caller never iterates."""
        try:
            self._cursor.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class TimedCollection:
    """Pass-through Collection wrapper. Logs FAIL only.

    Same reasoning as TimedCursor: a successful query is not an event worth
    a log record. A failed one is, and carries elapsed time and the verbatim
    Mongo response.
    """

    def __init__(self, coll: Any) -> None:
        self._coll = coll
        self._db_name = coll.database.name
        self._coll_name = coll.name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coll, name)

    def aggregate(self, pipeline, *args, **kwargs):
        start = time.monotonic()
        try:
            cursor = self._coll.aggregate(pipeline, *args, **kwargs)
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.aggregate FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "aggregate",
                                     self._db_name, self._coll_name)
            raise
        return TimedCursor(cursor, "aggregate", self._db_name, self._coll_name)

    def find(self, *args, batch_size: int | None = None, **kwargs):
        if batch_size is not None:
            kwargs["batch_size"] = batch_size
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        start = time.monotonic()
        try:
            cursor = self._coll.find(*args, **kwargs)
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.find FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "find",
                                     self._db_name, self._coll_name)
            raise
        return TimedCursor(cursor, "find", self._db_name, self._coll_name)

    def find_one(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        start = time.monotonic()
        try:
            result = self._coll.find_one(*args, **kwargs)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.find_one FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "find_one",
                                     self._db_name, self._coll_name)
            raise

    def count_documents(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        start = time.monotonic()
        try:
            result = self._coll.count_documents(*args, **kwargs)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.count_documents FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "count_documents",
                                     self._db_name, self._coll_name)
            raise


# THE naming convention for a versioned collection. One form, no variants:
#
#     <BaseName>_v_<N>        Provider_v_4, SpecialtyMetaData_v_4
#
# A collection is versioned if and only if its name ends this way. Anything
# else -- Users, sessions, DBVersions, and the retired provider_v03, which
# predates the convention -- is not versioned, is not resolved, and is read
# under exactly the name the caller gave.
#
# Absolute, because a rule that admits variants is a rule that has to guess.
# Guessing is what produced a map keyed by a base name derived two different
# ways for two generations of the same collection.
_VERSION_SEPARATOR = "_v_"


def _is_versioned(collection_name: str) -> tuple[bool, str]:
    """(is it versioned, its base name). The base is meaningless if not."""
    base, separator, generation = collection_name.rpartition(_VERSION_SEPARATOR)
    if not separator or not base or not generation.isdigit():
        return False, collection_name
    return True, base


# Built once from the bindings the runtime resolved at startup, and rebuilt
# only when those bindings change -- which is admin/swap and nothing else.
# A map rebuilt per query would read the binding state on every collection
# access in the process.
_VERSION_MAP: dict[tuple[str, str], str] | None = None
_VERSION_MAP_SOURCE: dict[str, str] | None = None


def _version_map() -> dict[tuple[str, str], str]:
    """{(database, base name): bound collection} for this runtime.

    Read from the bindings the runtime already holds, so this consults no
    second source and makes no round trip.

    Empty when nothing is bound, which is every devops tool and every
    pipeline: no binding means no version to manage, and the name given is
    the name used.
    """
    global _VERSION_MAP, _VERSION_MAP_SOURCE

    # Not wrapped. An exception here used to be swallowed into an empty map,
    # which reads as "nothing is versioned" and sends every caller to the
    # unversioned collection -- the precise failure this resolution exists to
    # prevent, arrived at by the code protecting itself. If the binding state
    # cannot be read, that is a fact the caller must see, not one to absorb.
    from .runtime_data_collections import _state
    bases = dict(_state.bases or {})

    if _VERSION_MAP is not None and _VERSION_MAP_SOURCE == bases:
        return _VERSION_MAP

    # The bases come from the binding record, which states them. Nothing is
    # split apart and nothing is inferred: the record says the base is
    # PublicHealthData.Provider and the version is 4, so this map holds
    # ("PublicHealthData", "Provider") -> "Provider_v_4" and the swap is a
    # lookup.
    built: dict[tuple[str, str], str] = {}
    for (db_name, base), fqn in bases.items():
        _, _, coll_name = str(fqn).partition(".")
        if db_name and base and coll_name:
            built[(db_name, base)] = coll_name

    _VERSION_MAP = built
    _VERSION_MAP_SOURCE = bases
    return built


class TimedDatabase:
    def __init__(self, db: Any, manage_versions: bool = True) -> None:
        self._db = db
        self._manage_versions = manage_versions

    def _resolve(self, name: str) -> str:
        """The collection this runtime should read for the name asked for.

        A caller names the collection it means -- SpecialtyMetaData -- and
        gets the version bound for this target and environment. Applications
        do not spell version numbers, so they cannot spell the wrong one:
        the homeopathic resolver named the unversioned collection directly
        and read 883 documents with no flags on them while the specialty
        filter, going through the binding, read the 884 that had them.

        Naming a version explicitly is refused rather than obeyed. A caller
        that writes Provider_v_3 while the binding says Provider_v_4 has
        stated something false about what this runtime reads, and serving it
        would mean two components in one process disagreeing about which
        generation of the data they are looking at -- silently, and
        differently per environment. It raises, so the first exercise of that
        path fails loudly instead of returning the wrong decade of data.
        """
        if not self._manage_versions:
            return name

        versioned, base = _is_versioned(name)
        bound = _version_map().get((self._db.name, base if versioned else name))

        if versioned:
            if bound is None:
                raise ChatHealthyException(
                    mode="config_error",
                    component="ChatHealthyMongoUtilities",
                    status_code=503,
                    fatal_error=True,
                    message=(
                        f"{self._db.name}.{name} names a version explicitly, but "
                        f"no version of {base!r} is bound for this runtime. Name "
                        f"the collection {base!r} and the binding decides the "
                        f"version, or construct "
                        f"ChatHealthyMongoUtilities(manage_versions=False) if "
                        f"addressing a specific generation is the job."),
                )
            if bound != name:
                raise ChatHealthyException(
                    mode="config_error",
                    component="ChatHealthyMongoUtilities",
                    status_code=503,
                    fatal_error=True,
                    message=(
                        f"{self._db.name}.{name} asks for a version this runtime "
                        f"does not read: the binding for {base!r} is {bound!r}. "
                        f"Name the collection {base!r} and let the binding "
                        f"answer, or construct "
                        f"ChatHealthyMongoUtilities(manage_versions=False) if "
                        f"addressing a specific generation is the job."),
                )
            return bound

        return bound or name

    def __getitem__(self, name: str) -> TimedCollection:
        return TimedCollection(self._db[self._resolve(name)])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class TimedClient:
    """Pass-through proxy over MongoClient that wraps every Collection
    returned via `client[db][coll]` so each op logs START + END/FAIL via
    ChatHealthyLoggingService. No time math - the asctime in each log
    line is the only timestamp."""

    def __init__(self, client: MongoClient, manage_versions: bool = True) -> None:
        self._client = client
        self._manage_versions = manage_versions

    def __getitem__(self, name: str) -> TimedDatabase:
        return TimedDatabase(self._client[name], self._manage_versions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class ChatHealthyMongoUtilities:
    """Canonical Mongo client provider. X.509 mTLS only.

    ONLY X.509 certificate-based authentication. No connection strings.
    No fallback auth paths. Every getConnection(cert_name) call:
      1. Fetches client cert+key from Key Vault using cert_name
      2. Verifies cert's Subject CN matches cert_name
      3. Fetches MongoDB's server CA cert from Key Vault
      4. Establishes mTLS connection with both client and server validation
      5. Returns TimedClient wrapping the authenticated MongoClient
    """

    def __init__(self, manage_versions: bool = True) -> None:
        """One mode, chosen here and nowhere else.

        manage_versions=True -- the default and what every application wants.
        A collection named by its plain name resolves to the version bound
        for this target and environment, so applications never spell a
        version number and therefore never spell the wrong one.

        manage_versions=False -- the name given is the name used. For the
        migration, the pipelines and any tool whose whole job is to address a
        specific generation of a collection.

        It is private and it is settable only here. A switch that could be
        flipped after construction would mean a handle's behaviour depends on
        when you looked at it, and the two modes read different data.
        """
        object.__setattr__(self, "_manage_versions", bool(manage_versions))

    def __setattr__(self, name: str, value: Any) -> None:
        """Block any attempt to assign connection state."""
        raise ChatHealthyException(
            mode="security_violation",
            message=f"ChatHealthyMongoUtilities does not accept attribute assignment. "
                    f"Call getConnection(cert_name) to establish a connection.",
            component="ChatHealthyMongoUtilities",
        )

    def _verify_certificate_names_the_identity(self, pem: str, identity: str) -> None:
        """The certificate's Subject CN must be the identity, exactly.

        Step 2 of this class's stated contract, which had no implementing code
        until 2026-08-16. Without it the identity argument selects a vault
        secret and nothing ever confirms the certificate inside names the same
        actor, so a mis-stored secret authenticates as whoever its certificate
        says and no caller can tell.
        """
        from cryptography.x509 import load_pem_x509_certificate  # noqa: PLC0415
        from cryptography.x509.oid import NameOID  # noqa: PLC0415

        try:
            cert = load_pem_x509_certificate(pem.encode("utf-8"))
            common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        except Exception as exc:
            raise ChatHealthyException(
                mode="security_violation",
                message=(f"certificate for {identity!r} could not be parsed: "
                         f"{type(exc).__name__}: {exc}"),
                component="ChatHealthyMongoUtilities",
                exception=exc) from exc

        if not common_names:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"certificate for {identity!r} carries no Subject CN",
                component="ChatHealthyMongoUtilities",
            )
        subject_cn = common_names[0].value
        if subject_cn != identity:
            raise ChatHealthyException(
                mode="security_violation",
                message=(f"certificate Subject CN {subject_cn!r} does not match "
                         f"identity {identity!r}"),
                component="ChatHealthyMongoUtilities",
            )

    def _vault_secret(self, vault_uri: str, token: str, name: str) -> str:
        """One secret out of Key Vault. Raises, never logs."""
        response = httpx.get(
            f"{vault_uri.rstrip('/')}/secrets/{name}",
            params={"api-version": "7.4"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if response.status_code != 200:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"Key Vault returned {response.status_code} for {name!r}",
                component="ChatHealthyMongoUtilities",
            )
        return response.json()["value"]

    def _azure_token(self, identity: str) -> str:
        """An Azure token for Key Vault, as this identity.

        The mechanism follows what the identity IS, not what happens to be
        in the environment. An identity that holds a client secret is an
        application registration and proves itself with that secret. An
        identity that holds none is a managed identity, proven by the host
        it is attached to through the instance metadata endpoint.

        The choice used to be made by whether IDENTITY_ENDPOINT was set.
        The Azure Automation sandbox sets that variable for every runbook,
        whether or not the account has a managed identity, so every runbook
        asked the metadata endpoint for a token as pipelineEditor -- an
        application registration that no host carries -- and took a 400
        with its own client secret sitting unread in the environment.
        """
        prefix = identity.strip().upper()
        client_secret = os.environ.get(f"{prefix}_AZURE_CLIENT_SECRET", "").strip()
        endpoint = (
            ""
            if client_secret
            else (os.environ.get("IDENTITY_ENDPOINT")
                  or os.environ.get("MSI_ENDPOINT"))
        )
        if endpoint:
            header = os.environ.get("IDENTITY_HEADER") or os.environ.get("MSI_SECRET", "")
            params = {
                "api-version": "2019-08-01",
                "resource": "https://vault.azure.net",
            }
            client_id = os.environ.get(f"{prefix}_AZURE_CLIENT_ID", "").strip()
            if client_id:
                params["client_id"] = client_id
            response = httpx.get(
                endpoint,
                params=params,
                headers={"X-IDENTITY-HEADER": header, "Metadata": "true"},
                timeout=15,
            )
            if response.status_code != 200:
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message=f"managed-identity token request for {identity!r} "
                            f"returned {response.status_code}",
                    component="ChatHealthyMongoUtilities",
                )
            return response.json()["access_token"]

        keys = (f"{prefix}_AZURE_TENANT_ID",
                f"{prefix}_AZURE_CLIENT_ID",
                f"{prefix}_AZURE_CLIENT_SECRET")
        try:
            tenant = os.environ[keys[0]].strip()
            client_id = os.environ[keys[1]].strip()
            client_secret = os.environ[keys[2]].strip()
        except KeyError as exc:
            # Name the condition, not the symptom. An identity this host is
            # not entitled to use has no credential here BY DESIGN -- that is
            # the control described above working. Reporting it as three
            # missing variables invites someone to go and add them, which is
            # granting a host an entitlement it was deliberately denied.
            #
            # Which of the two it is cannot be told apart here: an identity
            # that should be reachable and is misconfigured looks identical
            # to one this host must never hold. So the message says both, and
            # points at ChatHealthyConfig.CertificateRegistry, which is the
            # record that decides.
            raise ChatHealthyException(
                mode="vault_unreachable",
                message=f"this host holds no Azure credential for {identity!r}, "
                        f"so it cannot fetch that identity's certificate. Either "
                        f"the host is not entitled to act as {identity!r} -- in "
                        f"which case this is the entitlement model refusing, and "
                        f"the work belongs on a host that is -- or the identity "
                        f"is registered and this host is missing "
                        f"{', '.join(keys)}. ChatHealthyConfig.CertificateRegistry "
                        f"says which. Do not resolve this by reading the "
                        f"certificate out of the vault under another identity.",
                component="ChatHealthyMongoUtilities",
                exception=exc) from exc

        token_response = httpx.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://vault.azure.net/.default",
            },
            timeout=15,
        )
        if token_response.status_code != 200:
            raise ChatHealthyException(
                mode="vault_unreachable",
                message=f"Azure token request returned {token_response.status_code}",
                component="ChatHealthyMongoUtilities",
            )
        return token_response.json()["access_token"]

    def _fetch_identity_cert(self, identity: str) -> str:
        """Return the identity's cert+key PEM from Key Vault. Raises, never logs.

        Two logins happen to reach a collection, and they are different
        services with different credentials. This is the first: Azure Key
        Vault, to obtain the certificate. Mongo is the second, authenticated
        with that certificate.

        The Azure credential is named after the identity it fetches --
        `frontendUser` reads FRONTENDUSER_AZURE_*. No second argument: the
        Azure identity is not free to differ from the Mongo one, and a
        parameter that must always equal another parameter is a parameter
        that will eventually not. A host therefore holds exactly the vault
        credentials for the identities it is entitled to use, and one that
        is not entitled simply has no key and fails closed.

        This is what a single ambient AZURE_* credential cost: it served
        every identity, so the workstation's credential could read the front
        end's certificate and the front end's could read the pipeline's.

        Key Vault is a REST API and this is two HTTPS calls: a client-credentials
        token, then the secret. The Azure SDK does exactly this and costs tens of
        megabytes per image to do it.
        """
        vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
        if not vault_uri:
            raise ChatHealthyException(
                mode="vault_unreachable",
                message="KEY_VAULT_URI not set",
                component="ChatHealthyMongoUtilities",
            )
        token = self._azure_token(identity)

        try:
            pem = self._vault_secret(vault_uri, token, identity)
        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"No certificate in the vault for identity {identity!r}: {exc}",
                component="ChatHealthyMongoUtilities",
                exception=exc) from exc
        return pem.strip() + chr(10)

    def _vault_fact(self, cluster: str, identity: str, prefix: str) -> str:
        """One connection fact, off the TAGS of the identity's own cert secret.

        A separate secret per fact does not work and the 403 that proved it is
        the entitlement model being right: the vault grants each identity
        exactly one secret, its own certificate, so a shared mongo-host-* secret
        would need a grant per identity and would widen every identity's reach
        to read it.

        The tags on that one secret cost no extra grant and no extra round trip
        -- the certificate fetch already returns them. And it puts the fact
        where the operator said it goes: keyed by the cert name. An identity
        learns where the clusters IT may reach are, and nothing about the rest.
        """
        vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
        if not vault_uri:
            raise ChatHealthyException(
                mode="vault_unreachable",
                message="KEY_VAULT_URI not set, so no connection fact can be read",
                component="ChatHealthyMongoUtilities",
            )
        tags = self._vault_tags(vault_uri, identity)
        return (tags.get(f"{prefix}{cluster}") or "").strip()

    def _vault_tags(self, vault_uri: str, identity: str) -> dict:
        """The tags on an identity's certificate secret. Cached per process."""
        if identity in _TAG_CACHE:
            return _TAG_CACHE[identity]
        response = httpx.get(
            f"{vault_uri.rstrip('/')}/secrets/{identity}",
            params={"api-version": "7.4"},
            headers={"Authorization": f"Bearer {self._azure_token(identity)}"},
            timeout=15,
        )
        if response.status_code != 200:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"Key Vault returned {response.status_code} reading the "
                        f"connection facts tagged on {identity!r}",
                component="ChatHealthyMongoUtilities",
            )
        tags = response.json().get("tags") or {}
        _TAG_CACHE[identity] = tags
        return tags

    def _host_for(self, cluster: str, identity: str, host: str = "") -> str:
        """Where the cluster answers. Argument wins, then vault, then nothing.

        The argument exists for a caller that already knows -- it is not a
        default and it is not a fallback to something hardcoded, because there
        is nothing hardcoded left to fall back to.
        """
        if host:
            _HOST_CACHE[cluster] = host.strip()
            return _HOST_CACHE[cluster]
        if cluster in _HOST_CACHE:
            return _HOST_CACHE[cluster]
        found = self._vault_fact(cluster, identity, _VAULT_HOST_PREFIX)
        if found:
            _HOST_CACHE[cluster] = found
            return found
        raise ChatHealthyException(
            mode="manifest_incomplete",
            message=f"no host for cluster {cluster!r}: the vault holds no "
                    f"{_VAULT_HOST_PREFIX}{cluster} and no caller supplied one. "
                    f"This library holds no cluster facts.",
            component="ChatHealthyMongoUtilities",
        )

    def _connect_attempts(self, cluster: str, identity: str) -> int:
        """How many times to try. One, unless the vault says otherwise.

        A cluster that pauses between runs can be met mid-transition. Which
        cluster that is, is a deployment fact and not this library's to know.
        """
        raw = self._vault_fact(cluster, identity, _VAULT_ATTEMPTS_PREFIX)
        return int(raw) if raw.isdigit() and int(raw) > 0 else 1

    def _is_cacheable(self, cluster: str, identity: str) -> bool:
        """A client is reusable only against a cluster that stays up.

        Holding one against a cluster that pauses outlives the cluster, and an
        open handle is a false signal of activity to anything judging idleness.
        """
        return self._vault_fact(cluster, identity,
                           _VAULT_NO_CACHE_PREFIX).lower() not in ("1", "true", "yes")

    def getConnection(self, identity: str, cluster: str,
                      host: str = "") -> TimedClient:
        """Connect to a logical target as an identity, over mTLS.

        Args:
            identity: WHO. Selects the X.509 certificate, and therefore the
                MongoDB user and its grants.
            cluster: WHERE. An Atlas cluster name. The host it resolves to
                is read from CH_MONGO_HOST_<CLUSTER>, which the deploy sets;
                this library knows no cluster by name.

        There is no connection string and no password. The certificate IS the
        credential: the server matches its subject DN against a $external user
        and applies that user's role. An identity with no certificate, or whose
        certificate names a user the server does not know, cannot connect --
        which is the point.

        There is deliberately no fallback. If the certificate cannot be
        obtained this raises, because a fallback to a shared credential is the
        escape hatch that makes the whole model decorative.

        Raises:
            ChatHealthyException if either argument is invalid, the certificate
            cannot be obtained, or the connection fails.
        """
        if not identity or not isinstance(identity, str):
            raise ChatHealthyException(
                mode="security_violation",
                message=f"identity must be a non-empty string, got: {type(identity).__name__}",
                component="ChatHealthyMongoUtilities",
            )
        if not cluster or not isinstance(cluster, str):
            raise ChatHealthyException(
                mode="security_violation",
                message=f"cluster must be a non-empty Atlas cluster name, got: {cluster!r}",
                component="ChatHealthyMongoUtilities",
            )

        if self._is_cacheable(cluster, identity):
            cached = _CLIENT_CACHE.get((identity, cluster, self._manage_versions))
            if cached is not None:
                return cached

        cert_key_pem = self._fetch_identity_cert(identity)
        self._verify_certificate_names_the_identity(cert_key_pem, identity)
        # Per-process path, written whole then moved into place. Every worker
        # sharing an identity used to write one filename, and `open(..., "w")`
        # truncates: a process reading while another was mid-write got a
        # certificate whose private key had not been written yet, and the
        # handshake failed with "Private key doesn't match certificate". The
        # pid removes the sharing; the atomic replace means a reader sees a
        # whole file or none.
        cert_path = os.path.join(
            tempfile.gettempdir(),
            f"mongo_{identity}_{cluster}_{os.getpid()}.pem",
        )
        staging_path = f"{cert_path}.partial"
        with open(staging_path, "w", encoding="utf-8") as handle:
            handle.write(cert_key_pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(staging_path, 0o600)
        except OSError:
            pass
        os.replace(staging_path, cert_path)

        host = self._host_for(cluster, identity, host)
        uri = (
            f"mongodb+srv://{host}/?authSource=%24external"
            "&authMechanism=MONGODB-X509&retryWrites=true&w=majority"
        )
        # Name the trust store rather than inherit whatever the host has.
        # Atlas presents a chain the Azure Automation sandbox's own store
        # cannot verify -- every connection there failed with "unable to get
        # local issuer certificate" -- while a developer workstation verifies
        # it fine. certifi is the same bundle on both, so the handshake does
        # not depend on where the code happens to be running.
        import certifi  # noqa: PLC0415
        attempts = self._connect_attempts(cluster, identity)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                client = MongoClient(
                    uri,
                    tls=True,
                    tlsCertificateKeyFile=cert_path,
                    tlsCAFile=certifi.where(),
                    serverSelectionTimeoutMS=10000,
                )
                client.admin.command("ping")
                break
            except Exception as exc:
                last_exc = exc
                if attempt == attempts:
                    raise ChatHealthyException(
                        mode="mongo_network_failure",
                        message=(f"mTLS connection failed for {identity!r} on "
                                 f"{cluster!r} after {attempts} attempt(s): {exc}"),
                        component="ChatHealthyMongoUtilities",
                        exception=exc) from exc
                # No log here: this function raises, and the catcher logs.
                # The raised exception names the attempt count, so a retried
                # failure is still fully described where it is handled.
                time.sleep(_CONNECT_BACKOFF_SECONDS * attempt)

        timed = TimedClient(client, self._manage_versions)
        if self._is_cacheable(cluster, identity):
            # The mode is part of the key. Two handles to one cluster that
            # resolve names differently are not the same handle, and serving
            # a cached one across modes would hand a migration the
            # application's version or the reverse.
            _CLIENT_CACHE[(identity, cluster, self._manage_versions)] = timed
        return timed






