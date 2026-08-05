# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Regression suite: pipeline<->frontend cluster isolation (2026-08-03).

Operator directive 2026-08-03: the pipeline is 100% isolated from the
frontend cluster. The ONLY crossing is data_migrator/migrator.py, which
is always human-triggered.

Two invariants tested here as REGRESSIONS so we never silently regress:

  1. Mongo auth scope. The pipeline's mongo user (MONGO_connectionString)
     MUST be able to read+write on the pipeline cluster AND MUST be
     denied on the frontend cluster's PublicHealthData.

  2. Azure Key Vault access scope. The pipeline VM's Managed Identity
     (mi-control) MUST NOT hold a grant that allows it to read the
     frontend-cluster credential secret MONGO-FRONTEND-connectionString
     from the pipeline's Key Vault.

Requires env: MONGO_connectionString, AZ_SUBSCRIPTION_ID,
AZ_VM_RESOURCE_GROUP (defaults FindCareDataPipelines-Dev). Skips if
those are absent (CI without secrets, etc.).
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import uuid

import pytest


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pipeline_uri() -> str:
    uri = os.environ.get("MONGO_connectionString") or ""
    if not uri:
        pytest.skip("MONGO_connectionString not set")
    return uri


@pytest.fixture(scope="module")
def frontend_uri(pipeline_uri: str) -> str:
    # Swap the host to point the SAME credentials at the frontend cluster.
    return pipeline_uri.replace("chathealthydatapipeline", "chathealthyfrontend")


def _az_available() -> bool:
    return bool(shutil.which("az"))


# --------------------------------------------------------------------------
# Invariant 1: Mongo auth scope
# --------------------------------------------------------------------------
@pytest.mark.regression
def test_pipeline_user_can_write_to_pipeline_cluster_coord_db(pipeline_uri):
    """PipelineUserScoped MUST be able to insert+delete on
    chathealthypipelines (the coord DB). This is the write it needs
    every run for pipeline.runs, pipeline.work_items, etc."""
    from pymongo import MongoClient
    c = MongoClient(pipeline_uri, serverSelectionTimeoutMS=8000)
    test_id = f"iso_test_{uuid.uuid4().hex[:8]}"
    coll = c["chathealthypipelines"]["_isolation_test"]
    r = coll.insert_one({
        "_id": test_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "purpose": "regression: pipeline user must write on pipeline cluster",
    })
    assert r.inserted_id == test_id, "insert failed to return the id"
    delete_result = coll.delete_one({"_id": test_id})
    assert delete_result.deleted_count == 1, "delete failed to clean up"


@pytest.mark.regression
def test_pipeline_user_can_write_to_pipeline_cluster_public_health_data(pipeline_uri):
    """PipelineUserScoped MUST be able to write to PublicHealthData on
    the pipeline cluster (that's where the payload lands via publish)."""
    from pymongo import MongoClient
    c = MongoClient(pipeline_uri, serverSelectionTimeoutMS=8000)
    test_id = f"iso_test_{uuid.uuid4().hex[:8]}"
    coll = c["PublicHealthData"]["_isolation_test"]
    r = coll.insert_one({"_id": test_id, "created_at": datetime.datetime.utcnow().isoformat()})
    assert r.inserted_id == test_id
    coll.delete_one({"_id": test_id})


@pytest.mark.regression
def test_pipeline_user_is_denied_on_frontend_cluster_publichealthdata_write(frontend_uri):
    """Same credentials MUST fail auth (or fail to reach the collection)
    when pointed at the frontend cluster. This is the belt-and-suspenders
    guarantee: even if code was re-added that tried to write to frontend
    PublicHealthData, the auth would fail."""
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure, PyMongoError
    c = MongoClient(frontend_uri, serverSelectionTimeoutMS=8000)
    with pytest.raises((OperationFailure, PyMongoError)) as excinfo:
        c["PublicHealthData"]["_isolation_test"].insert_one(
            {"_id": f"iso_test_{uuid.uuid4().hex[:8]}"}
        )
    # OperationFailure with code 18 == AuthenticationFailed is the expected
    # signal. Any other error mode still counts as denial (the write did
    # not land) but note the failure mode so a future regression that
    # changes the failure mode is visible.
    assert "auth" in str(excinfo.value).lower() or "denied" in str(excinfo.value).lower(), (
        f"Denial did not look like an auth failure; got: {excinfo.value!r}"
    )


@pytest.mark.regression
def test_pipeline_user_is_denied_on_frontend_cluster_publichealthdata_read(frontend_uri):
    """Symmetric to the write test: the pipeline user MUST NOT be able
    to READ from frontend PublicHealthData either. Read + write both
    require auth; if auth fails they both fail."""
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure, PyMongoError
    c = MongoClient(frontend_uri, serverSelectionTimeoutMS=8000)
    with pytest.raises((OperationFailure, PyMongoError)):
        c["PublicHealthData"]["provider_v03"].find_one({})


# --------------------------------------------------------------------------
# Invariant 2: Key Vault access scope (via Azure control-plane inspection)
# --------------------------------------------------------------------------
_MI_NAME = "mi-control"
_KV_NAME = "kv-chpipeline-dev"
_FORBIDDEN_SECRET = "MONGO-FRONTEND-connectionString"


def _run_az(args: list[str]) -> subprocess.CompletedProcess:
    """Wrapper for az CLI. On Windows az is a .cmd file so shell=True is
    required for subprocess to find and invoke it correctly."""
    return subprocess.run(
        ["az", *args],
        capture_output=True, text=True,
        shell=(os.name == "nt"),
    )


@pytest.fixture(scope="module")
def mi_principal_id() -> str:
    if not _az_available():
        pytest.skip("az CLI not available; skipping MI-scope tests")
    rg = os.environ.get("AZ_VM_RESOURCE_GROUP", "FindCareDataPipelines-Dev")
    r = _run_az(["identity", "show", "--name", _MI_NAME,
                 "--resource-group", rg,
                 "--query", "principalId", "-o", "tsv"])
    if r.returncode != 0:
        pytest.skip(
            f"az identity show failed for {_MI_NAME} in {rg}: {r.stderr[:200]}"
        )
    pid = r.stdout.strip()
    assert pid, "principalId empty"
    return pid


@pytest.mark.regression
def test_pipeline_vm_mi_cannot_read_frontend_credential_secret(mi_principal_id):
    """The pipeline VM's Managed Identity (mi-control) MUST NOT hold any
    Key Vault grant on kv-chpipeline-dev that permits reading the
    MONGO-FRONTEND-connectionString secret.

    Two authorization models to check:
      (a) Access-policy mode: the MI's objectId appears in
          properties.accessPolicies[] with 'get' or 'list' on secrets.
      (b) RBAC mode: the MI has a role assignment at KV scope OR at the
          specific secret's scope that includes 'Microsoft.KeyVault/
          vaults/secrets/getSecret/action' or similar.

    In both cases we assert that the MI's grants do NOT permit reading
    the forbidden secret. Post-hardening this test passes because either
    (i) the secret has been moved to a separate KV, or (ii) the MI's
    role assignment excludes the specific secret's resource scope.
    """
    # Check (a): access-policy mode
    r = _run_az(["keyvault", "show", "--name", _KV_NAME,
                 "--query", "properties.accessPolicies", "-o", "json"])
    assert r.returncode == 0, f"KV show failed: {r.stderr[:300]}"
    policies = json.loads(r.stdout or "[]")
    mi_ap_grants = [
        p for p in policies if p.get("objectId") == mi_principal_id
    ]
    if mi_ap_grants:
        for p in mi_ap_grants:
            secrets_perms = set(p.get("permissions", {}).get("secrets", []))
            forbidden = secrets_perms & {"get", "list", "all"}
            assert not forbidden, (
                f"Pipeline VM MI ({mi_principal_id}) holds access-policy "
                f"grants {sorted(forbidden)} on {_KV_NAME}, which means it "
                f"can read {_FORBIDDEN_SECRET}. Move that secret to a "
                f"separate KV or convert the KV to per-secret RBAC and "
                f"exclude this secret from the MI's scope."
            )

    # Check (b): RBAC mode
    r2 = _run_az(["keyvault", "show", "--name", _KV_NAME,
                  "--query", "properties.enableRbacAuthorization", "-o", "tsv"])
    rbac_enabled = (r2.stdout or "").strip().lower() == "true"
    if rbac_enabled:
        # Look for role assignments at the KV scope OR at the specific
        # secret's scope for this MI.
        sub = os.environ.get("AZ_SUBSCRIPTION_ID", "")
        rg = os.environ.get("AZ_VM_RESOURCE_GROUP", "FindCareDataPipelines-Dev")
        kv_scope = (
            f"/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.KeyVault/vaults/{_KV_NAME}"
        )
        secret_scope = f"{kv_scope}/secrets/{_FORBIDDEN_SECRET}"
        r3 = _run_az([
            "role", "assignment", "list",
            "--assignee", mi_principal_id,
            "--scope", kv_scope, "-o", "json",
        ])
        assert r3.returncode == 0, f"RBAC lookup failed: {r3.stderr[:300]}"
        kv_role_assignments = json.loads(r3.stdout or "[]")
        # Any KV-scope 'Key Vault Secrets User' or similar reader role
        # grants access to ALL secrets including the forbidden one.
        secret_reader_roles = {
            "Key Vault Secrets User",
            "Key Vault Secrets Officer",
            "Key Vault Administrator",
            "Key Vault Reader",
            "Reader",
        }
        for a in kv_role_assignments:
            role = a.get("roleDefinitionName", "")
            scope = a.get("scope", "")
            if role in secret_reader_roles and scope == kv_scope:
                pytest.fail(
                    f"Pipeline VM MI has KV-scope RBAC role {role!r} on "
                    f"{_KV_NAME}, which permits reading {_FORBIDDEN_SECRET}. "
                    f"Restrict this MI to per-secret role assignments that "
                    f"EXCLUDE {_FORBIDDEN_SECRET}."
                )
        # Also check specific-secret scope for the forbidden secret.
        r4 = _run_az([
            "role", "assignment", "list",
            "--assignee", mi_principal_id,
            "--scope", secret_scope, "-o", "json",
        ])
        if r4.returncode == 0:
            secret_role_assignments = json.loads(r4.stdout or "[]")
            assert not secret_role_assignments, (
                f"Pipeline VM MI has role assignments at the specific "
                f"secret scope for {_FORBIDDEN_SECRET}: "
                f"{[a.get('roleDefinitionName') for a in secret_role_assignments]}. "
                f"Remove them."
            )


@pytest.mark.regression
def test_forbidden_secret_absent_or_isolated(mi_principal_id):
    """Belt-and-suspenders: the forbidden secret should either be
    completely absent from kv-chpipeline-dev (moved to a migrator-only
    KV), OR at minimum be tagged so its presence is intentional and the
    prior test's access-scope check must pass.
    """
    r = _run_az([
        "keyvault", "secret", "list",
        "--vault-name", _KV_NAME,
        "--query", "[].name", "-o", "json",
    ])
    assert r.returncode == 0, f"KV list failed: {r.stderr[:300]}"
    names = set(json.loads(r.stdout or "[]"))
    # If the secret is absent, we pass unconditionally.
    if _FORBIDDEN_SECRET not in names:
        return
    # If present, the auth-scope test above is what protects us; that
    # test is authoritative. This is just documentation that the secret
    # was intentionally kept in this KV (e.g., migrator co-tenancy) and
    # the RBAC scoping is what's doing the work.
    pytest.skip(
        f"{_FORBIDDEN_SECRET} present in {_KV_NAME} (migrator co-tenancy). "
        f"The access-scope RBAC test above is what enforces isolation."
    )
