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
CLUSTER_TARGETS = ("ChatHealthyFrontEnd", "ChatHealthyDataPipelines")

# A cluster maps to a HOST, never to a credentialed connection string. The
# credential comes from the identity's certificate and nowhere else, so no
# secret is involved in deciding where to connect.
#
# These are the PUBLIC hostnames, and they are the default because most
# callers need them: the operator workstation, and the Hugging Face Spaces
# that host the front-end services, are both outside Azure and can only reach
# Atlas over the internet. That reachability is a requirement, not debt.
#
# Azure compute is the exception. A VM, container-app job or runbook inside
# the pipeline vnet reaches the same clusters through the project's Azure
# Private Link service, and MUST, because traffic between our own Azure
# components has no business on a public address. Those hosts are not chosen
# here -- the deploy sets CH_MONGO_HOST_<CLUSTER> on the compute and this
# reads it. The library is told where to connect; it does not decide.
_TARGET_HOST = {
    "ChatHealthyFrontEnd": "chathealthyfrontend.mdwahg.mongodb.net",
    "ChatHealthyDataPipelines": "chathealthydatapipeline.mdwahg.mongodb.net",
}


def _host_for(cluster: str) -> str:
    return (os.environ.get(f"CH_MONGO_HOST_{cluster.upper()}") or "").strip() \
        or _TARGET_HOST[cluster]

# The data-pipeline cluster is paused whenever no run holds a reservation, so
# a connection to it can meet a machine mid-transition. It is not this
# library's job to wait out a wake: the runbook resumes the cluster to IDLE
# before it creates the host, so nothing downstream should ever meet a paused
# cluster. These attempts cover a brief transient, not a cold start -- a long
# envelope here would turn "is it up" into a multi-minute question for every
# caller that wants an answer now.
_CONNECT_ATTEMPTS = {
    "ChatHealthyFrontEnd": 1,
    "ChatHealthyDataPipelines": 3,
}
_CONNECT_BACKOFF_SECONDS = 5

# A client is reusable only for a cluster that stays up. Holding one against
# the pipeline cluster outlives the cluster itself, and an open handle is a
# false signal of activity to anything judging idleness.
_CACHEABLE_CLUSTERS = ("ChatHealthyFrontEnd",)

# The vault secret IS the identity: one secret, named exactly what the caller
# passes, holding the cert+key PEM. It was two secrets, cert-{identity} and
# key-{identity}, which the code fetched separately and immediately joined --
# two round trips to rebuild something only ever used whole, under two names
# neither of which was the identity.

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
            ) from exc

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
            raise ChatHealthyException(
                mode="vault_unreachable",
                message=f"{', '.join(keys)} are required to fetch the "
                        f"certificate for {identity!r}; {exc} missing",
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
            ) from exc
        return pem.strip() + chr(10)

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

        if cluster in _CACHEABLE_CLUSTERS:
            cached = _CLIENT_CACHE.get((identity, cluster))
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

        host = _host_for(cluster)
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
        attempts = _CONNECT_ATTEMPTS[cluster]
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
                    ) from exc
                # No log here: this function raises, and the catcher logs.
                # The raised exception names the attempt count, so a retried
                # failure is still fully described where it is handled.
                time.sleep(_CONNECT_BACKOFF_SECONDS * attempt)

        timed = TimedClient(client)
        if cluster in _CACHEABLE_CLUSTERS:
            _CLIENT_CACHE[(identity, cluster)] = timed
        return timed






