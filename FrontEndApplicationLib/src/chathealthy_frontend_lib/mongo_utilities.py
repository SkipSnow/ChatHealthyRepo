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

# Logical connection targets. A target names a PURPOSE, never a server:
#
#   frontEnd   user-facing data      PublicHealthData, Users
#   admin      operational records   ChatHealthyConfig, ClaudeCodeLog, Pipelines
#   pipelines  pipeline data         pipelinePublicHealthData
#
# The pipeline data factory is down by design most of the day, so anything
# that must answer 24x7 belongs to admin, not pipelines.
#
# Today all three resolve to the same physical cluster: the separation is
# real in the entitlement model and notional in the wiring. Buying the
# pipeline or admin cluster changes this map and nothing else.
CLUSTER_TARGETS = ("frontEnd", "admin", "pipelines")

# A target maps to a HOST, never to a credentialed connection string. The
# credential comes from the identity's certificate and nowhere else, so no
# secret is involved in deciding where to connect.
_TARGET_HOST = {
    "frontEnd": "chathealthyfrontend.mdwahg.mongodb.net",
    "admin": "chathealthyfrontend.mdwahg.mongodb.net",
    "pipelines": "chathealthydatapipeline.mdwahg.mongodb.net",
}

# Vault secret names are conventional: the identity IS the key. This removes
# the registry round-trip, which needed a Mongo connection to discover how to
# make a Mongo connection.
_VAULT_CERT_KEY = "cert-{identity}"
_VAULT_PRIVATE_KEY = "key-{identity}"

# One client per (identity, cluster). Certificate retrieval is a vault round
# trip; doing it per call would put it on every query.
_CLIENT_CACHE: dict[tuple[str, str], "TimedClient"] = {}


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


class TimedDatabase:
    def __init__(self, db: Any) -> None:
        self._db = db

    def __getitem__(self, name: str) -> TimedCollection:
        return TimedCollection(self._db[name])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class TimedClient:
    """Pass-through proxy over MongoClient that wraps every Collection
    returned via `client[db][coll]` so each op logs START + END/FAIL via
    ChatHealthyLoggingService. No time math - the asctime in each log
    line is the only timestamp."""

    def __init__(self, client: MongoClient) -> None:
        self._client = client

    def __getitem__(self, name: str) -> TimedDatabase:
        return TimedDatabase(self._client[name])

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

    def __init__(self) -> None:
        """No-op constructor. All state comes from vault. No caching."""
        pass

    def __setattr__(self, name: str, value: Any) -> None:
        """Block any attempt to assign connection state."""
        raise ChatHealthyException(
            mode="security_violation",
            message=f"ChatHealthyMongoUtilities does not accept attribute assignment. "
                    f"Call getConnection(cert_name) to establish a connection.",
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

    def _fetch_identity_cert(self, identity: str) -> str:
        """Return the identity's cert+key PEM from Key Vault. Raises, never logs.

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
        try:
            tenant = os.environ["AZURE_TENANT_ID"].strip()
            client_id = os.environ["AZURE_CLIENT_ID"].strip()
            client_secret = os.environ["AZURE_CLIENT_SECRET"].strip()
        except KeyError as exc:
            raise ChatHealthyException(
                mode="vault_unreachable",
                message=f"AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET "
                        f"are required; {exc} missing",
                component="ChatHealthyMongoUtilities",
            ) from exc

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
        token = token_response.json()["access_token"]

        try:
            cert = self._vault_secret(vault_uri, token, _VAULT_CERT_KEY.format(identity=identity))
            key = self._vault_secret(vault_uri, token, _VAULT_PRIVATE_KEY.format(identity=identity))
        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"No certificate in the vault for identity {identity!r}: {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc
        return cert.strip() + chr(10) + key.strip() + chr(10)

    def getConnection(self, identity: str, cluster: str) -> TimedClient:
        """Connect to a logical target as an identity, over mTLS.

        Args:
            identity: WHO. Selects the X.509 certificate, and therefore the
                MongoDB user and its grants.
            cluster: WHERE. A logical target from CLUSTER_TARGETS, which maps
                to a host.

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
        if cluster not in CLUSTER_TARGETS:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"cluster must be one of {CLUSTER_TARGETS}, got: {cluster!r}",
                component="ChatHealthyMongoUtilities",
            )

        cached = _CLIENT_CACHE.get((identity, cluster))
        if cached is not None:
            return cached

        cert_key_pem = self._fetch_identity_cert(identity)
        cert_path = os.path.join(
            tempfile.gettempdir(), f"mongo_{identity}_{cluster}.pem"
        )
        with open(cert_path, "w", encoding="utf-8") as handle:
            handle.write(cert_key_pem)
        try:
            os.chmod(cert_path, 0o600)
        except OSError:
            pass

        host = _TARGET_HOST[cluster]
        uri = (
            f"mongodb+srv://{host}/?authSource=%24external"
            "&authMechanism=MONGODB-X509&retryWrites=true&w=majority"
        )
        try:
            client = MongoClient(
                uri,
                tls=True,
                tlsCertificateKeyFile=cert_path,
                serverSelectionTimeoutMS=10000,
            )
            client.admin.command("ping")
        except Exception as exc:
            raise ChatHealthyException(
                mode="mongo_network_failure",
                message=f"mTLS connection failed for {identity!r} on {cluster!r}: {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc

        timed = TimedClient(client)
        _CLIENT_CACHE[(identity, cluster)] = timed
        return timed






