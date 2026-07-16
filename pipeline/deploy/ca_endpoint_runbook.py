"""ca_endpoint_runbook.py - hosts the ChatHealthy CA cert-issuance endpoint.

Runs in Azure Automation as a Python 3 runbook. Fired via ARM webhook by:
  - deploy_chathealthy.py at deploy time (long-lived cert provisioning
    for Runbook, Control, and future long-lived services)
  - Worker containers at boot time (short-lived self-mint path)

The runbook is invoked once per mint request. Its inputs (F-003 Design
v1 sec 4.1) arrive via WEBHOOKDATA:

  {
    "csr_pem":            "<PEM-encoded CSR>",
    "requested_subject":  "<CN string, echoed from the CSR subject>",
    "caller_principal":   "<managed-identity or SP display name>",
    "caller_ad_token":    "<Azure AD bearer token proving caller identity>"
  }

Response is written to output_json_path (WEBHOOKDATA extra field) OR
returned via the runbook job output stream:

  {
    "leaf_cert_pem":       "<PEM>",
    "intermediate_cert_pem":"<PEM>",   (chain-continuation for the consumer)
    "root_cert_pem":       "<PEM>"     (chain root, informational)
  }

Refusal per F-003 Design v1 sec 4.4 returns:
  {"error": "<reason>", "refused_at": "<step>"}

The private key is never returned - the caller generated it and holds
it in its own memory or (for long-lived deploy-time provisioning) the
deployer places it into KV under the target node's secret path.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ca_helpers as ch


LOG = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)


KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)
_HOSTNAME = socket.gethostname()


def _seed_azure_client_id_from_automation_variable() -> None:
    """Load AZURE_CLIENT_ID from the AA Automation Variable when unset.

    Required for user-assigned mi-runbook IMDS calls inside the sandbox.
    """
    if os.environ.get("AZURE_CLIENT_ID", "").strip():
        return
    try:
        import automationassets  # type: ignore
        value = automationassets.get_automation_variable("AZURE_CLIENT_ID")
    except Exception:
        return
    if value:
        # AA Variable REST stores JSON-encoded strings; strip accidental quotes.
        os.environ["AZURE_CLIENT_ID"] = str(value).strip().strip('"')


def _log_event(event: str, **fields) -> None:
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": "ca_endpoint_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    LOG.info(json.dumps(record, default=str))


def _parse_mint_input() -> dict:
    """Accept Automation webhook WEBHOOKDATA, sys.argv mint payload
    (deploy-time PUT /jobs), or a declared mint_request global.

    Azure Automation Python 3 delivers job parameter values as
    sys.argv[1..] (not named globals). Deploy-time mints pass a
    base64-encoded JSON blob to avoid AA's no-spaces-in-argv limit.
    WEBHOOKDATA remains the webhook path.
    """
    import base64

    def _coerce_payload(raw: str) -> dict:
        if not raw or not str(raw).strip():
            return {}
        text = str(raw).strip()
        # Prefer base64 (deploy-time); fall back to raw JSON (local tests).
        try:
            decoded = base64.b64decode(text, validate=True).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
        return {}

    if len(sys.argv) > 1 and sys.argv[1]:
        got = _coerce_payload(sys.argv[1])
        if got:
            return got

    mint_request = globals().get("mint_request")
    if mint_request:
        if isinstance(mint_request, dict):
            return mint_request
        if isinstance(mint_request, str):
            return _coerce_payload(mint_request)

    raw = os.environ.get("WEBHOOKDATA", "")
    if not raw:
        return {}
    parsed = json.loads(raw)
    body = parsed.get("RequestBody", "")
    if isinstance(body, str) and body:
        body = json.loads(body)
    return body or {}


def _verify_ad_token(token: str, expected_principal: str) -> None:
    """Validate the caller's Azure AD token really represents the claimed
    principal. Full JWT validation with JWKS is out of scope for the
    runbook script itself - Azure Automation runs behind Entra so the
    webhook layer would normally handle initial auth. The check here is
    a defense-in-depth structural check: the token MUST be a non-empty
    string; if empty we refuse. The mandatory production behavior is:

      - Runbook is fired only via authenticated webhook (Automation
        webhook URL includes a token that Azure validates before invoking).
      - Additional Entra token validation belongs in the webhook front
        door, not in this script.

    Raises Exception on refusal."""
    if not token or not isinstance(token, str) or len(token.strip()) < 16:
        raise Exception(
            f"caller_ad_token missing or too short for caller "
            f"'{expected_principal}'"
        )
    # Further validation (JWKS signature check, aud/iss claims, oid vs
    # expected principal) SHOULD be added here once Entra validation
    # substrate is wired. See F-003 Design v1 sec 4.4.


def _load_intermediate() -> tuple:
    """Read intermediate signing key + cert from KV. Only mi-runbook
    (the identity this runbook runs under) has RBAC read on the key."""
    tok = ch.imds_token("https://vault.azure.net")
    key_pem = ch.kv_get(KEY_VAULT_URI, ch.KV_SECRET_INTERMEDIATE_KEY, token=tok)
    cert_pem = ch.kv_get(KEY_VAULT_URI, ch.KV_SECRET_INTERMEDIATE_CERT, token=tok)
    if not key_pem or not cert_pem:
        raise Exception(
            "intermediate CA not present in KV; run ca_bootstrap_runbook first"
        )
    return ch.load_key_pem(key_pem.encode("ascii")), \
        ch.load_cert_pem(cert_pem.encode("ascii"))


def _load_root_cert_pem() -> str:
    """Public cert; returned in response so consumers can build the chain."""
    tok = ch.imds_token("https://vault.azure.net")
    pem = ch.kv_get(KEY_VAULT_URI, ch.KV_SECRET_ROOT_CA_CERT, token=tok)
    if not pem:
        raise Exception("root CA cert not present in KV")
    return pem


def _issue_leaf(csr_pem: bytes, caller_principal: str) -> dict:
    """Full mint path. Returns response dict on success, raises on refusal."""
    # 1. Parse + hygiene check the CSR.
    csr = ch.verify_csr(csr_pem)
    csr_cn = ch.extract_cn(csr)
    _log_event("csr_parsed", cn=csr_cn, caller=caller_principal)

    # 2. Namespace policy check.
    identity_class = ch.check_namespace_allowed(caller_principal, csr_cn)
    _log_event("namespace_check_passed",
               cn=csr_cn, caller=caller_principal,
               identity_class=identity_class)

    # 3. Load intermediate signing material.
    inter_key, inter_cert = _load_intermediate()
    _log_event("intermediate_loaded")

    # 4. Sign the leaf with the identity-class-appropriate validity.
    validity_days = ch.validity_days_for(identity_class)
    leaf = ch.sign_leaf(csr, inter_key, inter_cert, validity_days)
    _log_event("leaf_signed",
               cn=csr_cn,
               validity_days=validity_days,
               not_after=ch.cert_not_after_iso(leaf),
               serial=str(leaf.serial_number))

    return {
        "leaf_cert_pem": ch.cert_to_pem(leaf).decode("ascii"),
        "intermediate_cert_pem": ch.cert_to_pem(inter_cert).decode("ascii"),
        "root_cert_pem": _load_root_cert_pem(),
        "not_before": ch.cert_not_before_iso(leaf),
        "not_after": ch.cert_not_after_iso(leaf),
        "serial": str(leaf.serial_number),
    }


def _emit_response(payload: dict) -> None:
    """Two emit paths: (a) write to path in WEBHOOKDATA['response_path']
    if provided, (b) print a single tagged line to stdout so the caller
    can grep for it in the job's output stream."""
    body = os.environ.get("WEBHOOKDATA", "")
    response_path = None
    if body:
        try:
            outer = json.loads(body)
            inner = outer.get("RequestBody", "")
            if isinstance(inner, str) and inner:
                inner = json.loads(inner)
            response_path = (inner or {}).get("response_path")
        except Exception:
            response_path = None
    if response_path:
        with open(response_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    LOG.info("CA_RESPONSE " + json.dumps(payload))


def main() -> int:
    _seed_azure_client_id_from_automation_variable()
    _log_event("runbook_start")
    try:
        webhook = _parse_mint_input()
    except Exception as exc:
        _log_event("webhook_parse_failed",
                   error_type=type(exc).__name__,
                   error_msg=str(exc))
        _emit_response({"error": "mint payload not valid JSON",
                        "refused_at": "webhook_parse"})
        return 2

    if not webhook:
        _log_event("no_mint_input")
        _emit_response({"error": "no mint input (WEBHOOKDATA or mint_request)",
                        "refused_at": "webhook_parse"})
        return 2

    caller_principal = webhook.get("caller_principal", "")
    caller_token = webhook.get("caller_ad_token", "")
    csr_pem_str = webhook.get("csr_pem", "")

    if not csr_pem_str:
        _emit_response({"error": "csr_pem missing",
                        "refused_at": "input_validation"})
        return 2

    try:
        _verify_ad_token(caller_token, caller_principal)
    except Exception as exc:
        _log_event("ad_token_refused",
                   caller=caller_principal, error_msg=str(exc))
        _emit_response({"error": str(exc), "refused_at": "ad_token_verify"})
        return 3

    try:
        response = _issue_leaf(csr_pem_str.encode("ascii"), caller_principal)
    except ch.NamespaceRefused as exc:
        _log_event("mint_refused_namespace",
                   caller=caller_principal, error_msg=str(exc))
        _emit_response({"error": str(exc), "refused_at": "namespace_check"})
        return 3
    except Exception as exc:
        _log_event("mint_failed",
                   error_type=type(exc).__name__,
                   error_msg=str(exc),
                   traceback=traceback.format_exc()[-2000:])
        _emit_response({"error": str(exc), "refused_at": "mint"})
        return 1

    _emit_response(response)
    _log_event("runbook_exit_ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
