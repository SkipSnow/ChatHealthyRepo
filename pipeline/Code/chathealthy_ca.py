"""chathealthy_ca.py - CA client library.

Callable from:
  - deploy_chathealthy.py (deploy-time long-lived cert provisioning path;
    the deployer service principal authenticates)
  - Runbook scripts that need a fresh cert
  - container bootstrap.py (Worker self-mint path; the Worker's attached
    managed identity authenticates)

Two mint paths differ only in HOW the Azure AD token is acquired:
  - mint_cert(subject, azure_ad_token) - caller passes the token in
  - mint_via_managed_identity(subject, mi_credential) - caller passes a
    credential object; we call get_token on it

Both funnel to the same CaEndpointRunbook issuance API (F-003 §4).
Azure Automation webhooks are asynchronous (HTTP 202 + JobIds); the
client always polls the job and parses the CA_RESPONSE line from the
job output stream. Deploy-time minting may also PUT /jobs directly when
CHATHEALTHY_CA_AA_RG / CHATHEALTHY_CA_AA_NAME are set (no webhook needed).

Public helpers for chain trust:
  - load_ca_public_cert()    -> intermediate CA cert bytes (leaf's issuer)
  - load_intermediate_chain() -> concatenated intermediate + root PEM

Both public helpers can read from either:
  (a) the local filesystem when the container image was baked with the
      chain at /etc/chathealthy/ca/  (deploy-time flow, no runtime KV read)
  (b) Key Vault via managed identity, as a fallback for local dev where
      the image bake step didn't happen
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


LOG = logging.getLogger(__name__)


# Default CA chain locations baked into container images by the deployer.
CA_CHAIN_DIR = os.environ.get("CHATHEALTHY_CA_DIR", "/etc/chathealthy/ca")
CA_INTERMEDIATE_CERT_PATH = os.path.join(CA_CHAIN_DIR, "intermediate.pem")
CA_ROOT_CERT_PATH = os.path.join(CA_CHAIN_DIR, "root.pem")

# ARM webhook that fires ca_endpoint_runbook. Optional when AA job coords
# are provided via CHATHEALTHY_CA_AA_* instead.
CA_WEBHOOK_URL_ENV = "CHATHEALTHY_CA_WEBHOOK_URL"

# Deploy-time / MI path: start CaEndpointRunbook via PUT /jobs.
CA_AA_RG_ENV = "CHATHEALTHY_CA_AA_RG"
CA_AA_NAME_ENV = "CHATHEALTHY_CA_AA_NAME"
CA_AA_RUNBOOK_ENV = "CHATHEALTHY_CA_AA_RUNBOOK"
CA_AA_SUB_ENV = "CHATHEALTHY_CA_AA_SUBSCRIPTION"
CA_DEFAULT_RUNBOOK = "CaEndpointRunbook"

# KV secret names (mirror ca_helpers constants; duplicated here so this
# module remains importable from front-end code that never touches the
# deploy dir).
_KV_INTERMEDIATE_CERT = "ca-intermediate-cert"
_KV_ROOT_CERT = "ca-root-cert"

_AUTOMATION_API = "2023-11-01"


# ===========================================================================
# CSR construction (client-side)
# ===========================================================================
def _generate_client_keypair(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _build_csr(subject_cn: str,
               key: rsa.RSAPrivateKey) -> x509.CertificateSigningRequest:
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ChatHealthy.ai"),
        x509.NameAttribute(NameOID.COMMON_NAME, subject_cn),
    ])
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(key, hashes.SHA256())
    )


# ===========================================================================
# ARM helpers (Automation job start / poll / output)
# ===========================================================================
def _arm_request(method: str, url: str, token: str,
                 body: Optional[dict] = None,
                 timeout: int = 60) -> tuple[int, str]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _subscription_id(token: str) -> str:
    override = os.environ.get(CA_AA_SUB_ENV, "").strip()
    if override:
        return override
    # Resolve from the same credential's default subscription via ARM.
    # Prefer AZURE_SUBSCRIPTION_ID when the deployer already set it.
    env_sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if env_sub:
        return env_sub
    status, body = _arm_request(
        "GET",
        "https://management.azure.com/subscriptions?api-version=2020-01-01",
        token,
    )
    if status != 200:
        raise Exception(f"cannot list subscriptions for CA mint ({status}): {body[:400]}")
    vals = json.loads(body).get("value") or []
    if not vals:
        raise Exception("no Azure subscriptions visible to CA mint credential")
    return vals[0]["subscriptionId"]


def _aa_coords() -> tuple[str, str, str]:
    rg = os.environ.get(CA_AA_RG_ENV, "").strip()
    aa = os.environ.get(CA_AA_NAME_ENV, "").strip()
    runbook = os.environ.get(CA_AA_RUNBOOK_ENV, CA_DEFAULT_RUNBOOK).strip()
    if not rg or not aa:
        raise Exception(
            f"CA Automation coords not configured; set {CA_AA_RG_ENV} and "
            f"{CA_AA_NAME_ENV} (or {CA_WEBHOOK_URL_ENV} for webhook path)"
        )
    return rg, aa, runbook


def _start_ca_job_via_arm(csr_pem: bytes, subject_cn: str,
                          caller_principal: str,
                          azure_ad_token: str) -> str:
    """PUT /jobs for CaEndpointRunbook. Returns the AA job id."""
    rg, aa, runbook = _aa_coords()
    sub = _subscription_id(azure_ad_token)
    job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{job_id}?api-version={_AUTOMATION_API}"
    )
    mint_request = json.dumps({
        "csr_pem": csr_pem.decode("ascii"),
        "requested_subject": subject_cn,
        "caller_principal": caller_principal,
        "caller_ad_token": azure_ad_token,
    })
    body = {
        "properties": {
            "runbook": {"name": runbook},
            "parameters": {"mint_request": mint_request},
        }
    }
    status, resp = _arm_request("PUT", url, azure_ad_token, body=body, timeout=60)
    if status not in (200, 201):
        raise Exception(
            f"CA job PUT failed HTTP {status}: {resp[:800]}"
        )
    return job_id


def _start_ca_job_via_webhook(csr_pem: bytes, subject_cn: str,
                              caller_principal: str,
                              azure_ad_token: str) -> tuple[str, str, str]:
    """POST webhook; returns (job_id, rg, aa) for subsequent poll.
    Webhook response is HTTP 202 with JobIds; AA coords still required
    to poll and read the output stream."""
    webhook_url = os.environ.get(CA_WEBHOOK_URL_ENV, "").strip()
    if not webhook_url:
        raise Exception(f"{CA_WEBHOOK_URL_ENV} not set")
    rg, aa, _runbook = _aa_coords()
    payload = {
        "csr_pem": csr_pem.decode("ascii"),
        "requested_subject": subject_cn,
        "caller_principal": caller_principal,
        "caller_ad_token": azure_ad_token,
    }
    req = urllib.request.Request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status = r.status
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise Exception(
            f"CA webhook HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')[:500]}"
        ) from e
    if status not in (200, 202):
        raise Exception(f"CA webhook returned HTTP {status}: {body[:500]}")
    parsed = json.loads(body) if body.strip() else {}
    job_ids = parsed.get("JobIds") or parsed.get("jobIds") or []
    if not job_ids:
        # Synchronous response body (non-AA front door) — rare.
        if "leaf_cert_pem" in parsed or "error" in parsed:
            raise _SyncWebhookResponse(parsed)
        raise Exception(f"CA webhook response has no JobIds: {body[:500]}")
    return str(job_ids[0]), rg, aa


class _SyncWebhookResponse(Exception):
    """Internal: webhook returned a completed mint payload inline."""

    def __init__(self, payload: dict):
        super().__init__("sync webhook response")
        self.payload = payload


def _poll_job(rg: str, aa: str, job_id: str, token: str,
              timeout_sec: int = 300) -> dict:
    sub = _subscription_id(token)
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{job_id}?api-version={_AUTOMATION_API}"
    )
    deadline = time.time() + timeout_sec
    last = "?"
    while time.time() < deadline:
        status, body = _arm_request("GET", url, token, timeout=30)
        if status != 200:
            time.sleep(5)
            continue
        props = json.loads(body).get("properties") or {}
        last = props.get("status") or "?"
        if last in ("Completed", "Failed", "Stopped", "Suspended"):
            return {
                "status": last,
                "exception": props.get("exception") or "",
            }
        time.sleep(5)
    return {"status": f"Timeout(last={last})", "exception": ""}


def _job_output_text(rg: str, aa: str, job_id: str, token: str) -> str:
    """Fetch Automation job output + streams; return concatenated text."""
    sub = _subscription_id(token)
    chunks: list[str] = []
    out_url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{job_id}/output?api-version={_AUTOMATION_API}"
    )
    status, body = _arm_request("GET", out_url, token, timeout=60)
    if status == 200 and body:
        chunks.append(body)
    streams_url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{job_id}/streams?api-version={_AUTOMATION_API}"
    )
    status, body = _arm_request("GET", streams_url, token, timeout=60)
    if status == 200 and body:
        try:
            for item in (json.loads(body).get("value") or []):
                props = item.get("properties") or {}
                summary = props.get("summary") or props.get("streamText") or ""
                if summary:
                    chunks.append(str(summary))
        except json.JSONDecodeError:
            chunks.append(body)
    return "\n".join(chunks)


def _parse_ca_response(output_text: str) -> dict:
    marker = "CA_RESPONSE "
    for line in output_text.splitlines():
        idx = line.find(marker)
        if idx < 0:
            continue
        payload = line[idx + len(marker):].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        return parsed
    raise Exception(
        "CA job completed but no CA_RESPONSE line found in output. "
        f"Tail:\n{output_text[-1500:]}"
    )


def _invoke_issuance(csr_pem: bytes, subject_cn: str,
                     caller_principal: str,
                     azure_ad_token: str) -> dict:
    """Start CaEndpointRunbook, wait, return parsed CA_RESPONSE dict."""
    webhook = os.environ.get(CA_WEBHOOK_URL_ENV, "").strip()
    aa_rg = os.environ.get(CA_AA_RG_ENV, "").strip()
    if webhook and aa_rg:
        try:
            job_id, rg, aa = _start_ca_job_via_webhook(
                csr_pem, subject_cn, caller_principal, azure_ad_token,
            )
        except _SyncWebhookResponse as sync:
            return sync.payload
    elif aa_rg:
        rg, aa, _rb = _aa_coords()
        job_id = _start_ca_job_via_arm(
            csr_pem, subject_cn, caller_principal, azure_ad_token,
        )
    elif webhook:
        raise Exception(
            f"{CA_WEBHOOK_URL_ENV} is set but {CA_AA_RG_ENV}/"
            f"{CA_AA_NAME_ENV} are required to poll the AA job for the "
            f"signed leaf (Automation webhooks are asynchronous)."
        )
    else:
        raise Exception(
            f"CA issuance not configured; set {CA_AA_RG_ENV}+"
            f"{CA_AA_NAME_ENV} (deploy-time job path) and/or "
            f"{CA_WEBHOOK_URL_ENV}"
        )

    result = _poll_job(rg, aa, job_id, azure_ad_token)
    if result["status"] != "Completed":
        raise Exception(
            f"CA job {job_id} ended status={result['status']}: "
            f"{result['exception'][:800]}"
        )
    parsed = _parse_ca_response(
        _job_output_text(rg, aa, job_id, azure_ad_token)
    )
    if "error" in parsed:
        raise Exception(
            f"CA refused mint at step '{parsed.get('refused_at', '?')}': "
            f"{parsed['error']}"
        )
    if "leaf_cert_pem" not in parsed:
        raise Exception(f"CA response missing leaf_cert_pem: {parsed!r}"[:400])
    return parsed


# ===========================================================================
# Public mint API
# ===========================================================================
def mint_cert(subject: str,
              azure_ad_token: str,
              caller_principal: Optional[str] = None
              ) -> Tuple[bytes, bytes, str]:
    """Mint a leaf cert for `subject`.

    Returns (cert_pem, key_pem, not_after_iso).
    not_after_iso is the leaf's notAfter (UTC ISO-8601) for KV secret
    expiry attributes used by cert_placement rotation.
    """
    principal = caller_principal or os.environ.get("AZURE_CLIENT_ID", "")
    if not principal:
        raise Exception(
            "caller_principal not supplied and AZURE_CLIENT_ID env not set"
        )
    key = _generate_client_keypair()
    csr = _build_csr(subject, key)
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    response = _invoke_issuance(csr_pem, subject, principal, azure_ad_token)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    not_after = response.get("not_after") or ""
    if not not_after:
        leaf = x509.load_pem_x509_certificate(
            response["leaf_cert_pem"].encode("ascii")
        )
        if hasattr(leaf, "not_valid_after_utc"):
            not_after = leaf.not_valid_after_utc.isoformat()
        else:
            na = leaf.not_valid_after
            if na.tzinfo is None:
                import datetime as _dt
                na = na.replace(tzinfo=_dt.timezone.utc)
            not_after = na.isoformat()
    LOG.info("chathealthy_ca.mint_cert: minted cert for subject=%s "
             "not_after=%s serial=%s",
             subject, not_after, response.get("serial"))
    leaf_pem = response["leaf_cert_pem"]
    if isinstance(leaf_pem, str):
        leaf_pem = leaf_pem.encode("ascii")
    return leaf_pem, key_pem, not_after


def mint_via_managed_identity(subject: str,
                              mi_credential,
                              caller_principal: Optional[str] = None
                              ) -> Tuple[bytes, bytes, str]:
    """Same as mint_cert but pulls the AD token from a credential object
    (e.g. azure.identity.DefaultAzureCredential or ManagedIdentityCredential).
    Convenience wrapper used by container bootstrap.py."""
    token_obj = mi_credential.get_token("https://management.azure.com/.default")
    return mint_cert(subject, token_obj.token, caller_principal=caller_principal)


# ===========================================================================
# CA chain loaders (public certs)
# ===========================================================================
def load_ca_public_cert() -> bytes:
    """Return the intermediate CA cert PEM - the direct issuer of leaves.

    Prefers the disk copy baked at image build time. Falls back to KV
    read via managed identity (dev-only path; production images always
    have the chain on disk)."""
    if os.path.exists(CA_INTERMEDIATE_CERT_PATH):
        with open(CA_INTERMEDIATE_CERT_PATH, "rb") as f:
            return f.read()
    return _kv_read_public_cert(_KV_INTERMEDIATE_CERT)


def load_intermediate_chain() -> bytes:
    """Return intermediate || root PEM, in that order - the chain a
    consumer needs to verify a leaf back to the ChatHealthy root."""
    intermediate = load_ca_public_cert()
    if os.path.exists(CA_ROOT_CERT_PATH):
        with open(CA_ROOT_CERT_PATH, "rb") as f:
            root = f.read()
    else:
        root = _kv_read_public_cert(_KV_ROOT_CERT)
    return intermediate + b"\n" + root


def _kv_read_public_cert(secret_name: str) -> bytes:
    """Dev-fallback KV read of a public cert. Requires KEY_VAULT_URI env
    and either azure.identity or an IMDS-reachable token endpoint."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    vault_uri = os.environ.get(
        "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
    )
    cred = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_uri, credential=cred)
    return (client.get_secret(secret_name).value or "").encode("ascii")
