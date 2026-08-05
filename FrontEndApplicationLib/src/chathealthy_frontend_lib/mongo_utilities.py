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

from azure.identity import ClientSecretCredential
from azure.keyvault.secrets import SecretClient
from bson.json_util import dumps as bson_dumps
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
    """Pass-through cursor wrapper. Logs START / END / FAIL for the
    cursor iteration. Elapsed time is the asctime delta between START
    and END/FAIL - read it directly from the log; no time math here."""

    def __init__(self, cursor: Any, op: str, db: str, coll: str) -> None:
        self._cursor = cursor
        self._op = op
        self._db = db
        self._coll = coll

    def __iter__(self):
        log.debug("mongo.%s iter START db=%s coll=%s",
                  self._op, self._db, self._coll)
        start = time.monotonic()
        try:
            for doc in self._cursor:
                yield doc
            log.debug("mongo.%s iter END db=%s coll=%s",
                      self._op, self._db, self._coll)
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
    """Pass-through Collection wrapper. Each op logs START with the
    verbatim args sent to pymongo, and END or FAIL on completion. The
    duration is the asctime delta between START and END/FAIL."""

    def __init__(self, coll: Any) -> None:
        self._coll = coll
        self._db_name = coll.database.name
        self._coll_name = coll.name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coll, name)

    def aggregate(self, pipeline, *args, **kwargs):
        log.debug("mongo.aggregate START db=%s coll=%s pipeline=%s opts=%s",
                  self._db_name, self._coll_name, q(pipeline), q(kwargs))
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
        log.debug("mongo.find START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
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
        log.debug("mongo.find_one START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
        start = time.monotonic()
        try:
            result = self._coll.find_one(*args, **kwargs)
            log.debug("mongo.find_one END db=%s coll=%s hit=%s",
                      self._db_name, self._coll_name, result is not None)
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
        log.debug("mongo.count_documents START db=%s coll=%s filter=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(kwargs))
        start = time.monotonic()
        try:
            result = self._coll.count_documents(*args, **kwargs)
            log.debug("mongo.count_documents END db=%s coll=%s n=%d",
                      self._db_name, self._coll_name, result)
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

    def getConnection(self, cert_name: str) -> TimedClient:
        """Establish X.509 mTLS connection using named certificate.

        Args:
            cert_name: Name of the X.509 certificate in Key Vault.
                      Must match the certificate's Subject CN exactly.

        Returns:
            TimedClient wrapping an authenticated MongoClient.

        Raises:
            ChatHealthyException if cert not found, CN doesn't match,
            or MongoDB CA cert not found in vault.
        """
        if not cert_name or not isinstance(cert_name, str):
            raise ChatHealthyException(
                mode="security_violation",
                message=f"cert_name must be a non-empty string, got: {type(cert_name).__name__}",
                component="ChatHealthyMongoUtilities",
            )

        client_cert_key, mongo_uri, mongo_ca_cert = self._fetch_certs_and_uri(cert_name)
        self._verify_cert_subject_cn(client_cert_key, cert_name)
        uri_with_mtls = self._build_mtls_uri(cert_name, client_cert_key, mongo_uri, mongo_ca_cert)
        client = self._create_mtls_client(uri_with_mtls)
        return TimedClient(client)

    def _fetch_certs_and_uri(self, cert_name: str) -> tuple[str, str, str]:
        """Fetch client cert+key, MongoDB URI, and MongoDB CA cert from Key Vault.

        Args:
            cert_name: Name of the certificate (used as vault secret key).

        Returns:
            (client_cert_key_pem, mongo_uri, mongo_ca_cert_pem)

        Raises:
            ChatHealthyException if any secret not found or vault unreachable.
        """
        try:
            vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
            if not vault_uri:
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message="KEY_VAULT_URI environment variable not set",
                    component="ChatHealthyMongoUtilities",
                )

            tenant_id = os.environ.get("AZURE_TENANT_ID", "").strip()
            client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
            client_secret = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
            if not (tenant_id and client_id and client_secret):
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message="AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET must be set",
                    component="ChatHealthyMongoUtilities",
                )

            credential = ClientSecretCredential(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            )
            client = SecretClient(vault_url=vault_uri, credential=credential)

            secret_name = f"mongo-client-cert-key-{cert_name}"
            cert_secret = client.get_secret(secret_name)
            if not cert_secret or not cert_secret.value:
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message=f"Secret '{secret_name}' not found in vault",
                    component="ChatHealthyMongoUtilities",
                )

            uri_secret = client.get_secret("mongo-uri")
            if not uri_secret or not uri_secret.value:
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message="Secret 'mongo-uri' not found in vault",
                    component="ChatHealthyMongoUtilities",
                )

            ca_secret = client.get_secret("mongo-ca-cert")
            if not ca_secret or not ca_secret.value:
                raise ChatHealthyException(
                    mode="vault_unreachable",
                    message="Secret 'mongo-ca-cert' not found in vault",
                    component="ChatHealthyMongoUtilities",
                )

            return (cert_secret.value, uri_secret.value, ca_secret.value)

        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="vault_unreachable",
                message=f"Failed to fetch certs from vault for '{cert_name}': {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc

    def _verify_cert_subject_cn(self, client_cert_key_pem: str, expected_cn: str) -> None:
        """Verify that the certificate's Subject CN matches expected_cn.

        Args:
            client_cert_key_pem: Client certificate+key PEM string.
            expected_cn: Expected Common Name (CN) in certificate Subject.

        Raises:
            ChatHealthyException if CN doesn't match or cert is invalid.
        """
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend

            cert_pem = client_cert_key_pem.encode("utf-8")
            cert_obj = x509.load_pem_x509_certificate(cert_pem, default_backend())
            subject = cert_obj.subject
            cn_attr = subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)

            if not cn_attr:
                raise ChatHealthyException(
                    mode="security_violation",
                    message=f"Certificate has no Common Name (CN) in Subject",
                    component="ChatHealthyMongoUtilities",
                )

            cert_cn = cn_attr[0].value
            if cert_cn != expected_cn:
                raise ChatHealthyException(
                    mode="security_violation",
                    message=f"Certificate CN '{cert_cn}' does not match expected '{expected_cn}'",
                    component="ChatHealthyMongoUtilities",
                )

        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"Failed to verify certificate CN: {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc

    def _build_mtls_uri(
        self,
        cert_name: str,
        client_cert_key_pem: str,
        mongo_uri: str,
        mongo_ca_cert_pem: str,
    ) -> str:
        """Build MongoDB URI with mTLS configuration.

        Writes client cert+key and server CA cert to temp files, builds URI
        with X.509 authMechanism and paths to both certs.

        Args:
            cert_name: Certificate name (for temp file naming).
            client_cert_key_pem: Client cert+key PEM.
            mongo_uri: Base MongoDB URI (without auth params).
            mongo_ca_cert_pem: MongoDB server CA cert PEM.

        Returns:
            Complete MongoDB URI with X.509 parameters.
        """
        try:
            temp_dir = tempfile.gettempdir()
            client_cert_path = os.path.join(temp_dir, f"mongo_client_{cert_name}.pem")
            ca_cert_path = os.path.join(temp_dir, f"mongo_ca_{cert_name}.pem")

            with open(client_cert_path, "w") as f:
                f.write(client_cert_key_pem)
            with open(ca_cert_path, "w") as f:
                f.write(mongo_ca_cert_pem)

            separator = "&" if "?" in mongo_uri else "?"
            uri_with_mtls = (
                f"{mongo_uri}"
                f"{separator}authMechanism=MONGODB-X509"
                f"&authSource=%24external"
                f"&tlsCertificateKeyFile={client_cert_path}"
                f"&tlsCAFile={ca_cert_path}"
            )
            return uri_with_mtls

        except Exception as exc:
            raise ChatHealthyException(
                mode="security_violation",
                message=f"Failed to build mTLS URI: {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc

    def _create_mtls_client(self, uri_with_mtls: str) -> MongoClient:
        """Create MongoClient with mTLS configuration.

        Args:
            uri_with_mtls: MongoDB URI with X.509 and TLS cert parameters.

        Returns:
            Authenticated MongoClient ready for queries.

        Raises:
            ChatHealthyException on connection failure.
        """
        try:
            kwargs = {"timeoutMS": TIMEOUT_MS}

            appname = os.environ.get("CHATHEALTHY_SERVICE_NAME")
            if appname:
                kwargs["appname"] = appname

            client = MongoClient(uri_with_mtls, **kwargs)
            client.admin.command("ping")
            return client

        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="mongo_network_failure",
                message=f"Failed to establish mTLS connection: {exc}",
                component="ChatHealthyMongoUtilities",
            ) from exc
