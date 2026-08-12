"""FrontEndUser least-privilege conformance.

The expectation table below IS the audit
(architecture/IdentityCertificatesAndAuthentications/ArchitectureDesignAndAuditDocs/
FrontEnd_DB_Analysis_2026-08-07.xlsx). A failure means the deployed Atlas role and
the audit disagree -- either the role is wrong, or the audit is.

NO PRODUCTION COLLECTION IS NAMED, READ, OR WRITTEN BY THIS TEST.

Every probe targets `security_test`, a collection of 100 dummy rows provisioned
by a db admin via _oneshots/provision_security_test_collections.py. Real rows
matter: "read is permitted" only means something if a document comes back, and
"write is permitted" only means something if the write lands on a document.

Databases whose grant is collection-scoped (admin.Versions,
ChatHealthyConfig.DBVersions, Pipelines.Log) are asserted DENY. `security_test`
is not one of the granted collections, so a refusal there proves the grant did
not widen to the whole database -- without going near the granted collection.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection():
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", 'frontEnd')

ALLOW = "ALLOW"
DENY = "DENY"

ENVS = ("local", "dev", "qa", "prod")

# Provisioned by a db admin outside this test. Never an application collection.
COLLECTION = "security_test"
SEEDED_ROWS = 100

# (database, expected_read, expected_write)
EXPECTED: list[tuple[str, str, str]] = [
    # Database-level readWrite per the audit.
    ("Users", ALLOW, ALLOW),
    ("PublicHealthData", ALLOW, ALLOW),
    # Collection-scoped grants. The database as a whole must stay closed.
    ("ChatHealthyConfig", DENY, DENY),   # FIND on DBVersions only
    ("admin", DENY, DENY),               # FIND on Versions only
    ("Pipelines", DENY, DENY),           # INSERT on Log only
    # Pipeline data and environment-segregated leftovers: no access at all.
    ("chathealthypipelines", DENY, DENY),
    ("qa_PublicHealthData", DENY, DENY),
    ("pytest_smd_scratch_PublicHealthData", DENY, DENY),
    ("ClaudeCodeLog", DENY, DENY),
    ("dev_System", DENY, DENY),
    ("qa_System", DENY, DENY),
]

for _env in ENVS:
    EXPECTED += [
        (f"{_env}_Safety", ALLOW, ALLOW),
        (f"{_env}_Debug", ALLOW, ALLOW),
        (f"{_env}_admin", ALLOW, ALLOW),
        (f"{_env}_AboutUs", ALLOW, ALLOW),
    ]

DATABASES = [db for db, _, _ in EXPECTED]


@pytest.fixture(scope="module")
def client():
    load_dotenv()
    # retryWrites off: an unauthorised write is retried by default, which turns
    # every DENY probe into a multi-second wait for a verdict already known.
    c = _ch_connection()
    yield c
    c.close()


def _attempt(fn):
    """Return (verdict, result). An authorisation failure is the signal;
    anything else is a broken probe and must surface, not be swallowed."""
    try:
        return ALLOW, fn()
    except OperationFailure as exc:
        if exc.code == 13 or "not authorized" in str(exc):
            return DENY, None
        raise


def _ids(case) -> str:
    db, r, w = case
    return f"{db}[r={r},w={w}]"


@pytest.mark.parametrize("case", EXPECTED, ids=_ids)
def test_read_privilege_matches_audit(client, case):
    db, expected_read, _ = case
    verdict, doc = _attempt(lambda: client[db][COLLECTION].find_one({"seq": 0}))
    assert verdict == expected_read, (
        f"READ {db}: audit says {expected_read}, cluster says {verdict}"
    )
    if verdict == ALLOW:
        assert doc is not None, (
            f"READ {db}: permitted, but no seeded row came back. Either "
            f"{db}.{COLLECTION} was never provisioned or it is empty, so this "
            f"assertion proved nothing. Run "
            f"_oneshots/provision_security_test_collections.py"
        )


@pytest.mark.parametrize("case", EXPECTED, ids=_ids)
def test_write_privilege_matches_audit(client, case):
    db, _, expected_write = case
    verdict, result = _attempt(
        lambda: client[db][COLLECTION].update_one({"seq": 0}, {"$set": {"touched": True}})
    )
    assert verdict == expected_write, (
        f"WRITE {db}: audit says {expected_write}, cluster says {verdict}"
    )
    if verdict == ALLOW:
        assert result.matched_count == 1, (
            f"WRITE {db}: permitted, but matched no document. The write did not "
            f"land on a seeded row, so this assertion proved nothing."
        )


@pytest.mark.parametrize("db", [d for d, r, _ in EXPECTED if r == ALLOW])
def test_seeded_rows_are_all_visible(client, db):
    """The readable databases must show the full seed, not a partial view."""
    verdict, count = _attempt(lambda: client[db][COLLECTION].count_documents({}))
    assert verdict == ALLOW
    assert count == SEEDED_ROWS, (
        f"{db}.{COLLECTION}: expected {SEEDED_ROWS} seeded rows, saw {count}"
    )


def test_no_database_outside_the_audit_is_reachable(client):
    """Over-granting check: any database the audit does not name must be closed."""
    named = set(DATABASES)
    try:
        present = set(client.list_database_names())
    except OperationFailure:
        pytest.skip("listDatabases not granted -- cannot enumerate")

    reachable = [
        db for db in sorted(present - named)
        if _attempt(lambda: client[db][COLLECTION].find_one({}))[0] == ALLOW
    ]
    assert not reachable, (
        "readable but absent from the audit: " + ", ".join(reachable)
    )
