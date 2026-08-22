"""Daily entitlement report.

An operational report, read every morning to run the estate. It answers four
questions about every identity that holds rights: what it is, who classified
it, who answers for it, and what it can reach. Anything it cannot establish it
says it cannot establish, rather than printing a figure that reads as clean.

Every fact on the page is measured at the time stated. The subscriptions come
from what the reporting identity can see, the classification from directory
group membership, the delegation from directory ownership, the privilege of a
role from the actions Azure publishes for it, and where certificate material
lives from the vaults themselves. Nothing is asserted from this file.

That is not a stylistic preference. The population was once a dict written
here, matched by hand to what Azure held, so the report printed zero exceptions
by construction and an identity added to the estate appeared nowhere at all.

CLI:
    python entitlement_report.py [--no-email] [--out <path>]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

# On a workstation the repository is present and the library is imported from
# it. Inside Azure Automation there is no git tree: the library is inlined into
# the runbook and _root stays None, which the manifest reader treats as "the
# deployment architecture is not reachable from here".
_root: Path | None = None
for _d in Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _root = _d
        for _p in (_d / "ChatHealthyLib" / "src", _d / "pipeline" / "Code"):
            if str(_p) not in sys.path:
                sys.path.insert(0, str(_p))
        break

import os as _ch_os
_ch_os.environ.setdefault("CH_LOG_DESTINATION", "stderr")
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

import requests  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import landscape, letter  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfgen import canvas as canvas_module  # noqa: E402
from reportlab.platypus import (CondPageBreak,   # noqa: E402
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether,
)

_LOG = ChatHealthyLoggingService()

# Azure Automation keeps a runbook's settings as Automation Variables, which are
# not environment variables until the runbook asks for them. Outside Automation
# the import fails and the environment already holds what is needed.
try:
    import automationassets  # type: ignore[import-not-found]

    for _k in ("DEVOPSUSER_AZURE_TENANT_ID",
               "DEVOPSUSER_AZURE_CLIENT_ID",
               "DEVOPSUSER_AZURE_CLIENT_SECRET",
               "SPARKMAIL_API_KEY",
               "NOTIFICATION_FROM_EMAIL",
               "ENTITLEMENT_REPORT_TO_EMAIL"):
        try:
            _ch_os.environ[_k] = str(automationassets.get_automation_variable(_k))
        except Exception:                                       # noqa: BLE001
            pass
except ImportError:
    pass

ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#c8c8c8")
BAND = colors.HexColor("#f2f2f2")
FLAG = colors.HexColor("#a3231d")
OK = colors.HexColor("#1f6b34")

def _approved_register(source: str = "") -> tuple[dict[str, tuple], str]:
    """The approved population, read from the register the build baked.

    This used to be a dict written into this file. That made the audit grade
    itself against its own answer key: the literal was authored to match what
    Azure held, so the report printed zero exceptions by construction, and an
    identity added to the estate appeared nowhere at all.

    The register is derived from IdentityCatalog in deployment_architecture.json
    at build time and shipped beside this runbook, because Azure Automation has
    no git tree. So the approved population changes only by deploying a changed
    manifest -- and a manifest change to IdentityCatalog needs operator approval
    under Rule-065-ENF-005 -- while the observed population is enumerated live
    on every run. The two are never the same source.

    A missing register is fatal. Falling back to a built-in list would restore
    exactly the defect this replaces, and a report that cannot say what was
    approved must not print a number that reads as though it could.
    """
    # Inside Azure Automation the runbook is a single file: nothing staged
    # beside it is deployed, so the build injects the register as a module
    # global instead. This file carries no population of its own; the value is
    # derived from IdentityCatalog at build time on every build.
    injected = globals().get("_BAKED_IDENTITY_REGISTER")
    if injected:
        register = {}
        for e in injected.get("identities", []):
            oid = (e.get("object_id") or "").strip()
            if not oid:
                continue
            register[oid] = (
                e.get("identity_id", ""),
                e.get("description", ""),
                e.get("actor_type", ""),
                e.get("entra_object_type", "") or e.get("identity_class", ""),
                e.get("application", ""),
                tuple(e.get("roles", []) or ()),
            )
        src = injected.get("source") or {}
        where = (f"{src.get('environment','?')} build {src.get('build','?')} "
                 f"commit {src.get('commit','?')}")
        return register, where

    # Named explicitly, the manifest is fetched from that environment rather
    # than read from whatever happens to be on disk. A report run against dev
    # while reading a workstation`s edits states a register nobody deployed.
    if source:
        url = (f"https://{source}.chathealthy.ai/schemas/deployment_architecture.json"
               if not source.startswith("http") else source)
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            raise ChatHealthyException(
                mode="config_error", component="entitlement_report",
                message=f"cannot fetch the identity register from {url}: "
                        f"HTTP {r.status_code}")
        entries = r.json().get("IdentityCatalog", [])
        register = {}
        for e in entries:
            oid = (e.get("object_id") or "").strip()
            if oid:
                register[oid] = (
                    e.get("identity_id", ""), e.get("description", ""),
                    e.get("actor_type", ""),
                    e.get("entra_object_type", "") or e.get("identity_class", ""),
                    e.get("application", ""), tuple(e.get("roles", []) or ()))
        return register, url

    here = Path(__file__).resolve().parent
    candidates = [here / "entitlement_report_identity_register.json"]
    if _root is not None:
        candidates.append(_root / "brain" / "machine_artifacts" / "content"
                          / "deployment_architecture.json")
    for path in candidates:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("identities") or data.get("IdentityCatalog") or []
        register = {}
        for e in entries:
            oid = (e.get("object_id") or "").strip()
            if not oid:
                continue
            register[oid] = (
                e.get("identity_id", ""),
                e.get("description", ""),
                e.get("actor_type", ""),
                e.get("entra_object_type", "") or e.get("identity_class", ""),
                e.get("application", ""),
                tuple(e.get("roles", []) or ()),
            )
        return register, f"{path.name}, working tree"
    from chathealthy_lib.exceptions import ChatHealthyException
    raise ChatHealthyException(
        mode="config_error",
        component="entitlement_report",
        message=("no identity register found. Expected "
                 "entitlement_report_identity_register.json beside this file "
                 "(baked by the build) or deployment_architecture.json in the "
                 "repository. The report will not run against a built-in list."))


# The read is separated from the logging of it: a function that raises does
# not also log, because the catcher logs and not the thrower.
_REGISTER_ARG = ""
for _i, _a in enumerate(sys.argv):
    if _a == "--register-from" and _i + 1 < len(sys.argv):
        _REGISTER_ARG = sys.argv[_i + 1]
    elif _a.startswith("--register-from="):
        _REGISTER_ARG = _a.split("=", 1)[1]
APPROVED, _REGISTER_SOURCE = _approved_register(_REGISTER_ARG)
_LOG.info("entitlement_report approved register read from %s (%d identities)",
          _REGISTER_SOURCE, len(APPROVED))

# Roles that confer administrative authority over the subscription or over
# who may hold rights within it. Their presence is the thing an auditor looks
# for first, so they are counted and marked wherever they appear.
# What makes a role privileged, stated as the actions that make it so. A typed
# list of role names was a fact copied into this file: it went stale the moment a
# role was renamed -- "ChatHealthy Agent" became "chatHealthyAgent" and the list
# would have stopped flagging the most powerful role in the estate while still
# printing a confident table. These patterns are matched against each role's own
# published actions, so a role is privileged because of what it permits.
PRIVILEGE_MARKERS = (
    "*",
    "microsoft.authorization/roleassignments/write",
    "microsoft.authorization/roleassignments/delete",
    "microsoft.authorization/roledefinitions/write",
    "microsoft.authorization/roledefinitions/delete",
    "microsoft.authorization/elevateaccess/action",
    "microsoft.managedidentity/userassignedidentities/write",
    "microsoft.keyvault/vaults/accesspolicies/write",
)

_SECTION_TITLE = None
_SECTION_NOTE = None

SECTIONS = (
    ("Scope", "the tenant and each subscription, and whether this run could "
              "enumerate it"),
    ("Population and exceptions", "how many principals hold rights and how "
                                  "many are in the approved register"),
    ("Full entitlement detail", "every right held by each principal, and the "
                                "scope at which it is granted"),
    ("What each right permits", "every right named here, stated from the "
                                "actions the role publishes"),
    ("Where a grant can land", "each resource group and the subscription it "
                               "belongs to"),
    ("Exceptions", "everything wanting a decision: principals outside the "
                   "register, grants whose principal is gone, resources with "
                   "no description, shared secrets, and grants that add "
                   "nothing"),
    ("Group definitions", "each directory group, what it means and who "
                          "manages it"),
    ("Vault-wide access", "principals that can reach every secret in a vault, "
                          "and whether they can write"),
)


def _section_header(number, suffix=""):
    """Kept for callers that want the header as one string."""
    label, line = SECTIONS[number - 1]
    return f"{number}: {label}{suffix}"


def _section_block(number, count=""):
    """The header as three stacked lines, per the specified layout.

    The number and title lead in blue at heading size. Beneath them, indented
    and italic, sit the sentence that says what the section states and the
    count of what it found. One line carrying all three read as a paragraph
    and the eye had nothing to land on.

    A CondPageBreak precedes it so a header is never left at the foot of a
    page with its section overleaf: if less than an inch and a half remains,
    the section starts on the next page instead.
    """
    label, line = SECTIONS[number - 1]
    # A header needs its section under it. Three inches is the header
    # itself plus several lines of whatever follows; with less than that
    # left, the section starts on the next page rather than stranding
    # its title at the foot of this one.
    out = [CondPageBreak(3.0 * inch),
           Paragraph(f"{number}: {label}", _SECTION_TITLE),
           Paragraph(line, _SECTION_NOTE)]
    if count:
        out.append(Paragraph(count, _SECTION_NOTE))
    return out


def _scope_story(data: dict) -> list[str]:
    """One paragraph: what the report states, and where each fact comes from."""
    vaults = data["vaults"]
    certs = data["certificate_secrets"]
    where = (f"one key vault, {vaults[0]}" if len(vaults) == 1
             else f"{len(vaults)} key vaults ({', '.join(vaults)})" if vaults
             else "no key vault visible to this run")
    return [
        f"This report states every identity holding rights in the subscriptions named "
        f"above, what each may do, how it is classified and who manages it. "
        f"Role assignments and role definitions come from Azure Resource Manager. "
        f"Classification comes from directory group membership and management from "
        f"directory ownership. Whether a role is administrative is read from the "
        f"actions Azure publishes for it. The approved population comes from "
        f"IdentityCatalog in deployment_architecture.json. {len(certs)} certificate "
        f"secrets were found, in {where}. All values are read at the time stated."
    ]


def _control_policy(data: dict) -> str:
    """Who holds administrative roles, and what each one's roles permit.

    This sentence used to end "which includes creating and entitling further
    identities", appended to whatever the derivation found. It was prose, and it
    was false for DevOpsUser, whose administrative flag comes from a key vault
    data wildcard that confers no power over identities at all. Each principal
    now carries the clauses computed from the actions of the roles it actually
    holds.
    """
    lines = []
    for h in data["holders"]:
        admin = [g["role"] for g in h["grants"]
                 if g["role"] in data["privileged_roles"]]
        if not admin:
            continue
        clauses = []
        for role in sorted(set(admin)):
            for c in data["role_reach"].get(role, {}).get("reach", []):
                if c not in clauses:
                    clauses.append(c)
        lines.append(f"{h['name'] or h['object_id']} holds "
                     f"{', '.join(sorted(set(admin)))}, permitting "
                     f"{'; '.join(clauses)}.")
    if not lines:
        lines.append("No principal holds an administrative role.")
    lines.append("Classification below is directory group membership."
                 if data["groups_readable"] else
                 "The directory could not be read on this run, so nothing below "
                 "is classified.")
    return " ".join(lines)


def _population_sentence(data: dict) -> str:
    """The counts, composed from what was actually found. Volatile facts do not
    belong in fixed prose: the register changes, and a report that states a
    number it did not measure is wrong the day after it is written."""
    approved = [h for h in data["holders"] if h["approved"]]
    absent = data["approved_absent"]

    def _n(n: int, one: str, many: str) -> str:
        return f"{n} {one}" if n == 1 else f"{n} {many}"

    all_h = data["holders"]
    all_humans = [h for h in all_h if h["type"].lower() == "user"]
    all_components = [h for h in all_h if h["type"].lower() != "user"]
    outside = [h for h in all_h if not h["approved"]]

    parts = [f"{_n(len(all_h), 'identity holds', 'identities hold')} rights in this subscription"]
    if all_humans and all_components:
        parts[0] += (f": {_n(len(all_humans), 'named person', 'named people')} and "
                     f"{_n(len(all_components), 'component', 'components')}.")
    else:
        parts[0] += "."
    parts.append(
        f"{_n(len(approved), 'appears', 'appear')} in the approved register and "
        f"{_n(len(outside), 'does', 'do')} not.")
    if absent:
        parts.append(
            f"{_n(len(absent), 'identity in the register holds', 'identities in the register hold')} "
            f"no rights here: {', '.join(absent)}.")
    return " ".join(parts)


class _Credential:
    """A token, fetched directly from the Microsoft identity platform.

    Client-credentials is one HTTP POST, so the report carries no SDK. That
    keeps its dependencies to requests and reportlab, both of which install
    cleanly on the Automation Account's Python, and removes four libraries
    whose versions would have to be tracked for no benefit.
    """

    def __init__(self, tenant: str, client_id: str, client_secret: str):
        self._url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        self._id = client_id
        self._secret = client_secret
        self._cache: dict[str, str] = {}

    def token(self, resource: str) -> str:
        if resource in self._cache:
            return self._cache[resource]
        r = requests.post(self._url, timeout=60, data={
            "grant_type": "client_credentials",
            "client_id": self._id,
            "client_secret": self._secret,
            "scope": f"{resource}/.default",
        })
        if r.status_code != 200:
            raise ChatHealthyException(
                mode="azure_login_failed",
                component="EntitlementReport",
                message=f"token request for {resource} returned "
                        f"{r.status_code}: {r.text[:200]}",
                context={"resource": resource, "status": r.status_code})
        self._cache[resource] = r.json()["access_token"]
        return self._cache[resource]

    def get_token(self, scope: str):
        """Shaped like the SDK's credential so call sites read the same."""
        return type("T", (), {"token": self.token(scope.rsplit("/.default", 1)[0])})()


def _credential() -> _Credential:
    """The identity the report runs as: DevOpsUser, and nothing else.

    It ran as pipelineEditor, which sees one subscription and is confined to the
    pipeline by design. The report must be able to see every subscription it
    claims to cover, and DevOpsUser is the identity that deploys across them.
    Giving pipelineEditor that reach instead would widen the pipeline runtime to
    subscriptions it has no business in.
    """
    keys = ("DEVOPSUSER_AZURE_TENANT_ID",
            "DEVOPSUSER_AZURE_CLIENT_ID",
            "DEVOPSUSER_AZURE_CLIENT_SECRET")
    v = {k: _ch_os.environ.get(k, "") for k in keys}
    if not all(v.values()):
        try:
            if _root is None:
                raise ImportError
            from dotenv import dotenv_values
            local = dotenv_values(_root / ".env")
            v = {k: (v[k] or (local.get(k) or "")) for k in keys}
        except ImportError:
            pass
    missing = [k for k in keys if not v[k]]
    if missing:
        raise ChatHealthyException(
            mode="azure_credential_missing",
            component="EntitlementReport",
            message=f"cannot authenticate as DevOpsUser: {', '.join(missing)} absent",
            context={"missing": missing})
    return _Credential(v[keys[0]], v[keys[1]], v[keys[2]])


def _get_all(url: str, token: str) -> list[dict]:
    out: list[dict] = []
    headers = {"Authorization": f"Bearer {token}"}
    while url:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 200:
            raise ChatHealthyException(
                mode="azure_query_failed",
                component="EntitlementReport",
                message=f"{r.status_code} from {url.split('?')[0]}: {r.text[:300]}",
                context={"status": r.status_code})
        payload = r.json()
        out.extend(payload.get("value", []))
        url = payload.get("nextLink") or payload.get("@odata.nextLink") or ""
    return out


def _principal_names(object_ids: list[str], credential) -> dict[str, dict]:
    try:
        token = credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("graph token unavailable: %s", exc)
        return {}
    resolved: dict[str, dict] = {}
    for i in range(0, len(object_ids), 900):
        r = requests.post(
            f"{GRAPH}/directoryObjects/getByIds",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            data=json.dumps({"ids": object_ids[i:i + 900]}), timeout=60)
        if r.status_code != 200:
            _LOG.info("graph getByIds returned %s", r.status_code)
            return {}
        for o in r.json().get("value", []):
            resolved[o["id"]] = {
                "name": o.get("displayName") or o.get("userPrincipalName") or o["id"],
                "kind": o.get("@odata.type", "").rsplit(".", 1)[-1],
                "record": "live",
                "qualities": {},
            }

    # The same lookup continues into the directory's deleted items. An object
    # id either has a record or it does not; where that record lives is one of
    # the qualities it comes back with, not a case to be tested for.
    for kind in ("microsoft.graph.user", "microsoft.graph.servicePrincipal",
                 "microsoft.graph.group"):
        page = (f"{GRAPH}/directory/deletedItems/{kind}"
                f"?$select=id,displayName,userPrincipalName,userType,"
                f"createdDateTime,deletedDateTime,accountEnabled")
        while page:
            d = requests.get(page, headers={"Authorization": f"Bearer {token}"},
                             timeout=60)
            if d.status_code != 200:
                break
            payload = d.json()
            for o in payload.get("value", []):
                if o["id"] in resolved or o["id"] not in set(object_ids):
                    continue
                deleted = o.get("deletedDateTime") or ""
                recoverable = ""
                if deleted:
                    try:
                        when = _dt.datetime.fromisoformat(deleted.replace("Z", "+00:00"))
                        recoverable = (when + _dt.timedelta(days=30)).date().isoformat()
                    except ValueError:
                        recoverable = ""
                resolved[o["id"]] = {
                    "name": o.get("displayName") or o.get("userPrincipalName") or o["id"],
                    "kind": kind.rsplit(".", 1)[-1],
                    "record": "deleted",
                    "qualities": {
                        "sign-in name": o.get("userPrincipalName", ""),
                        "kind": o.get("userType", ""),
                        "created": (o.get("createdDateTime") or "")[:10],
                        "deleted": deleted[:10],
                        "recoverable until": recoverable,
                        "account was enabled": ("yes" if o.get("accountEnabled")
                                                else "no") if o.get("accountEnabled")
                                               is not None else "",
                    },
                }
            page = payload.get("@odata.nextLink") or ""
    return resolved


def _managed_identity_names(token: str, subscription_ids: list[str]) -> dict[str, dict]:
    """Resolve user-assigned managed identities through ARM.

    Managed identities are Azure resources as well as directory principals, so
    their names are readable without any Graph permission. This is what keeps
    the report legible when directory read is unavailable.
    """
    out: dict[str, dict] = {}
    items: list[dict] = []
    for sid in subscription_ids:
        try:
            items.extend(_get_all(
                f"{ARM}/subscriptions/{sid}/providers"
                f"/Microsoft.ManagedIdentity/userAssignedIdentities?api-version=2023-01-31",
                token))
        except Exception as exc:                                # noqa: BLE001
            _LOG.info("managed identity enumeration failed in %s: %s", sid, exc)
    for i in items:
        pid = (i.get("properties") or {}).get("principalId")
        if pid:
            out[pid] = {"name": i.get("name", pid), "kind": "ManagedIdentity"}
    return out


# A role that contains another: holding the key grants everything the values
# grant, so a narrower assignment alongside it adds nothing.
def _role_containment(defs: list[dict]) -> dict[str, set[str]]:
    """Which roles subsume which, computed from the actions Azure publishes.

    This was a typed map of four entries. It was a fact about Azure copied into
    this file: correct for the roles somebody thought of, silent about every
    other pair, and unable to notice that a custom role had been widened to
    swallow another. Now a role contains another when its action set covers the
    other's, wildcards expanded, which is what "already covered by" means.
    """
    def expand(d: dict) -> tuple[set[str], set[str]]:
        acts, data_acts = set(), set()
        for perm in (d["properties"].get("permissions") or []):
            acts.update(a.strip().lower() for a in (perm.get("actions") or []))
            data_acts.update(a.strip().lower() for a in (perm.get("dataActions") or []))
        return acts, data_acts

    def covers(wide: set[str], narrow: set[str]) -> bool:
        if not narrow:
            return False
        for needed in narrow:
            if needed in wide:
                continue
            if any(w == "*" or (w.endswith("*") and needed.startswith(w[:-1]))
                   for w in wide):
                continue
            return False
        return True

    sets = {d["properties"]["roleName"]: expand(d) for d in defs}
    out: dict[str, set[str]] = {}
    for wide_name, (wa, wd) in sets.items():
        for narrow_name, (na, nd) in sets.items():
            if wide_name == narrow_name:
                continue
            if covers(wa, na) and (not nd or covers(wd, nd)):
                out.setdefault(wide_name, set()).add(narrow_name)
    return out


def _secret_descriptions() -> dict[str, str]:
    """What each vault secret is, read from the vault that holds it.

    The description is a tag on the secret. It used to be read from
    deployment_architecture.json, which meant the report could describe a
    secret the vault does not hold and miss one it does, and could say so
    with the same confidence either way. The manifest is written by the same
    hands the report audits, so it is no longer a source here; the vault is.

    A secret carrying no description renders with its name alone. That
    silence is now a measurable gap rather than an invisible one.
    """
    out: dict[str, str] = {}
    for vault in _vault_hosts():
        for name, described in _secret_tags(vault).items():
            if described:
                out[name] = described
    return out


_DESCRIPTION_TAGS = ("description", "Description", "purpose", "Purpose")


def _vault_hosts() -> list[str]:
    """Every vault this run can reach, from the environment that names them."""
    hosts: list[str] = []
    uri = _ch_os.environ.get("KEY_VAULT_URI", "").strip()
    if uri:
        hosts.append(uri.rstrip("/"))
    return hosts


def _secret_tags(vault_uri: str) -> dict[str, str]:
    """Every secret in one vault and what its own tags say it is.

    A vault that cannot be listed contributes nothing and does not stop the
    report: the count it prints is what states the coverage.
    """
    found: dict[str, str] = {}
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        token = DefaultAzureCredential().get_token(
            "https://vault.azure.net/.default").token
    except Exception as exc:  # noqa: BLE001 - an unreadable vault is not a crash
        _LOG.info("vault %s unreadable, secrets render unnamed: %s", vault_uri, exc)
        return found
    url = f"{vault_uri}/secrets?api-version=7.4"
    while url:
        try:
            response = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            _LOG.info("vault %s listing failed: %s", vault_uri, exc)
            return found
        if response.status_code != 200:
            _LOG.info("vault %s listing returned %s", vault_uri,
                      response.status_code)
            return found
        body = response.json()
        for item in body.get("value", []):
            name = item.get("id", "").rsplit("/", 1)[-1]
            if not name:
                continue
            tags = item.get("tags") or {}
            for key in _DESCRIPTION_TAGS:
                if tags.get(key):
                    found[name] = tags[key]
                    break
            found.setdefault(name, "")
        url = body.get("nextLink")
    return found


def _reaches(scope: str, subscription_name: str, contents: dict | None = None) -> str:
    """The thing a grant reaches, named as a thing.

    A scope is a path, and rendering its levels as columns made the report a
    picture of Azure's addressing scheme. What an operator needs is what the
    grant can touch: a vault, a secret, a container, a subscription. Each is a
    first-class object with a type and a name, and that is what this returns.
    """
    low = scope.lower()
    for marker, label in (
            ("/secrets/", "secret"),
            ("/containers/", "blob container"),
            ("/providers/microsoft.keyvault/vaults/", "key vault"),
            ("/providers/microsoft.storage/storageaccounts/", "storage account"),
            ("/providers/microsoft.containerregistry/registries/", "container registry"),
            ("/providers/microsoft.automation/automationaccounts/", "automation account"),
            ("/providers/microsoft.managedidentity/userassignedidentities/",
             "managed identity")):
        if marker in low:
            name = scope[low.index(marker) + len(marker):].split("/", 1)[0]
            return f"{label} {name}"
    if "/resourcegroups/" in low:
        name = scope[low.index("/resourcegroups/") + len("/resourcegroups/"):].split("/", 1)[0]
        return f"resource group {name}"
    if not scope.strip("/"):
        return "the whole tenant"
    return f"every resource in subscription {subscription_name}"


def _holds(scope: str, subscription_name: str, contents: dict | None = None) -> str:
    """How much sits inside the thing a grant reaches.

    Its own column: a count is not a name, and a reader comparing two grants
    should not have to read past a resource description to find it.
    """
    if scope.strip("/") and "/subscriptions/" in scope.lower() and (
            "/resourcegroups/" in scope.lower() or "/providers/" in scope.lower()):
        return ""
    c = (contents or {}).get(subscription_name)
    if not c:
        return ""
    vaults = (f", {c['vaults']} key vault{'s' if c['vaults'] != 1 else ''}"
              if c["vaults"] else "")
    return f"{c['count']} resource{'s' if c['count'] != 1 else ''}{vaults}"


def _scope_parts(scope: str) -> dict:
    """Split a scope path into the things it names.

    A scope is a nested path and each level is a different object: the
    subscription, the resource group inside it, the resource inside that, and
    the object the grant reaches. Rendered as one string they read as a single
    fact, and the object -- a secret, which IS the credential -- ended up at the
    tail of a long line. Each level is returned separately so each gets its own
    column.
    """
    out = {"resource_group": "", "resource": "", "object": ""}
    low = scope.lower()
    if "/resourcegroups/" in low:
        rest = scope[low.index("/resourcegroups/") + len("/resourcegroups/"):]
        out["resource_group"] = rest.split("/", 1)[0]
    for marker in ("/providers/Microsoft.KeyVault/vaults/",
                   "/providers/Microsoft.Storage/storageAccounts/",
                   "/providers/Microsoft.ContainerRegistry/registries/",
                   "/providers/Microsoft.Automation/automationAccounts/",
                   "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"):
        if marker.lower() in low:
            rest = scope[low.index(marker.lower()) + len(marker):]
            out["resource"] = rest.split("/", 1)[0]
            break
    if "/secrets/" in low:
        out["object"] = scope[low.index("/secrets/") + len("/secrets/"):]
    elif "/containers/" in low:
        out["object"] = scope[low.index("/containers/") + len("/containers/"):]
    return out


def _scope_label(scope: str, subscription_ids: tuple[str, ...] = ()) -> str:
    """The scope, named by the thing it governs rather than by its full path.

    The distinguishing element goes last and must not be buried: two grants that
    differ only in which secret they cover have to read as different rows.
    """
    s = scope
    for sid in subscription_ids:
        s = s.replace(f"/subscriptions/{sid}", "")
    if not s.strip():
        return "the whole subscription"
    if s.strip() == "/":
        # A grant at the tenant root is not a grant on this subscription; it is
        # above it, and covers every subscription the tenant will ever hold.
        return "the tenant root -- above every subscription"
    s = s.replace("/resourceGroups/", "resource group ")
    s = s.replace("/providers/Microsoft.KeyVault/vaults/", ", key vault ")
    s = s.replace("/providers/Microsoft.ContainerRegistry/registries/", ", container registry ")
    s = s.replace("/providers/Microsoft.Storage/storageAccounts/", ", storage account ")
    s = s.replace("/providers/Microsoft.Automation/automationAccounts/", ", automation account ")
    s = s.replace("/providers/Microsoft.ManagedIdentity/userAssignedIdentities/",
                  ", managed identity ")
    s = s.replace("/providers/Microsoft.ManagedIdentity/userAssignedIdentities/", ", managed identity ")
    s = s.replace("/secrets/", ", the single secret ")
    return s.strip()


def _mark_redundant(grants: list[dict],
                    subscription_ids: tuple[str, ...] = (),
                    role_contains: dict[str, set[str]] | None = None) -> None:
    """Flag any grant already covered by a broader one the same identity holds.

    Two ways one grant covers another: the same role at a scope that contains
    this one, or a role that contains this role at a containing scope. A grant
    so covered permits nothing additional, and saying so is the difference
    between an inventory and a review.
    """
    for g in grants:
        for other in grants:
            if other is g:
                continue
            wider_scope = (other["raw_scope"] != g["raw_scope"]
                           and g["raw_scope"].startswith(other["raw_scope"]))
            same_scope = other["raw_scope"] == g["raw_scope"]
            covers_role = (other["role"] == g["role"]
                           or g["role"] in (role_contains or {}).get(other["role"], ()))
            if covers_role and (wider_scope or (same_scope and other["role"] != g["role"])):
                g["redundant"] = (f"already covered by {other['role']} on "
                                  f"{_scope_label(other['raw_scope'], subscription_ids)}")
                break


def _all_principals(token: str, credential, subscription_id: str) -> dict[str, dict]:
    """Every principal that exists, whether or not it holds anything.

    A principal can be brought into existence holding nothing -- creating a
    virtual machine with a system-assigned identity mints one as a side effect
    of the resource write. Such a principal appears in no role assignment, so a
    report built from assignments alone cannot see it until somebody grants it
    something. This enumerates existence rather than entitlement.

    Managed identities and anything attached to a resource come from Azure
    itself. Users and application registrations come from the directory, and
    are absent when the reporting identity has no directory read -- which the
    report then says rather than implying the list is complete.
    """
    found: dict[str, dict] = {}

    for item in _get_all(
            f"{ARM}/subscriptions/{subscription_id}/providers"
            f"/Microsoft.ManagedIdentity/userAssignedIdentities?api-version=2023-01-31",
            token):
        pid = (item.get("properties") or {}).get("principalId")
        if pid:
            found[pid] = {"name": item.get("name", pid), "kind": "ManagedIdentity",
                          "origin": "user-assigned identity"}

    for res in _get_all(
            f"{ARM}/subscriptions/{subscription_id}/resources"
            f"?api-version=2021-04-01&$expand=identity", token):
        ident = res.get("identity") or {}
        pid = ident.get("principalId")
        if pid and pid not in found:
            found[pid] = {"name": f"{res.get('name', '?')} (system-assigned)",
                          "kind": "ManagedIdentity",
                          "origin": f"attached to {res.get('type', 'a resource')}"}
        for uid in (ident.get("userAssignedIdentities") or {}):
            upid = ((ident["userAssignedIdentities"][uid]) or {}).get("principalId")
            if upid and upid not in found:
                found[upid] = {"name": uid.rsplit("/", 1)[-1], "kind": "ManagedIdentity",
                               "origin": "user-assigned identity"}

    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("directory not enumerable: %s", exc)
        return found
    # Microsoft's own first-party service principals live in every tenant --
    # Azure Cloud Shell, Azure Compute, a hundred more. They are not this firm's
    # identities, nobody here granted them anything, and listing them buries the
    # handful that matter under pages nobody reads. A principal is ours when the
    # tenant that owns its application is this one; Microsoft's carry Microsoft's.
    tenant = _tenant_id()
    for kind, url in (("ServicePrincipal",
                       f"{GRAPH}/servicePrincipals"
                       f"?$select=id,displayName,servicePrincipalType,appOwnerOrganizationId"),
                      ("User", f"{GRAPH}/users?$select=id,displayName,userPrincipalName")):
        page = url
        while page:
            r = requests.get(page, headers={"Authorization": f"Bearer {gtoken}"}, timeout=60)
            if r.status_code != 200:
                _LOG.info("directory enumeration of %s returned %s", kind, r.status_code)
                break
            payload = r.json()
            for o in payload.get("value", []):
                if o["id"] in found:
                    continue
                owner = o.get("appOwnerOrganizationId")
                if kind == "ServicePrincipal" and owner and tenant and owner != tenant:
                    continue
                found[o["id"]] = {
                    "name": o.get("displayName") or o.get("userPrincipalName") or o["id"],
                    "kind": o.get("servicePrincipalType", kind),
                    "origin": "directory",
                }
            page = payload.get("@odata.nextLink") or ""
    return found


def _directory_roles(credential) -> dict[str, list[str]]:
    """Directory roles held, by principal object id.

    Tenant-level authority is not an Azure role assignment and cannot be found
    by reading Azure. Global Administrator is a directory role, it sits above
    every subscription the tenant holds, and a report that reads only the
    resource plane concludes there is no tenant owner while one exists. Both
    planes are read here for that reason.
    """
    out: dict[str, list[str]] = {}
    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("directory roles not readable: %s", exc)
        return out
    headers = {"Authorization": f"Bearer {gtoken}"}
    defs = requests.get(f"{GRAPH}/roleManagement/directory/roleDefinitions"
                        f"?$select=id,displayName", headers=headers, timeout=60)
    if defs.status_code != 200:
        _LOG.info("directory role definitions returned %s", defs.status_code)
        return out
    names = {d["id"]: d["displayName"] for d in defs.json().get("value", [])}
    ras = requests.get(f"{GRAPH}/roleManagement/directory/roleAssignments",
                       headers=headers, timeout=60)
    if ras.status_code != 200:
        _LOG.info("directory role assignments returned %s", ras.status_code)
        return out
    for a in ras.json().get("value", []):
        pid = a.get("principalId")
        role = names.get(a.get("roleDefinitionId"), a.get("roleDefinitionId", ""))
        if pid:
            out.setdefault(pid, []).append(role)
    return out


def _owners(credential, object_ids: dict[str, str]) -> dict[str, list[str]]:
    """Who is accountable for each object, read from the directory.

    Authority in this estate is delegated, not flat: a person empowers an agent,
    and that agent then administers others. Entra records that as the owner edge,
    and it is the only place the delegation is written down -- an entitlement
    table shows what an identity may do, never who answers for it existing.

    Both type casts are queried. Graph's default owners collection omits service
    principals, so an agent that owns objects appears to own nothing, and a
    delegation to an agent reads as no delegation at all.
    """
    out: dict[str, list[str]] = {}
    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("owners not readable: %s", exc)
        return out
    headers = {"Authorization": f"Bearer {gtoken}"}
    for oid, kind in object_ids.items():
        names: list[str] = []
        for cast in ("microsoft.graph.user", "microsoft.graph.servicePrincipal"):
            r = requests.get(f"{GRAPH}/{kind}/{oid}/owners/{cast}?$select=displayName",
                             headers=headers, timeout=60)
            if r.status_code != 200:
                continue
            names.extend(o.get("displayName", "") for o in r.json().get("value", []))
        if names:
            out[oid] = sorted(n for n in names if n)
    return out


def _vaults_and_certificates(token: str, subscription_ids: list[str]) -> tuple[list[str], list[str]]:
    """Where certificate material actually lives, and how much of it there is.

    Named rather than asserted: the count and the vault names are what make the
    scope paragraph a measurement instead of a claim. A secret is treated as
    certificate material when a role assignment in this estate points at it and
    its name is carried by the identity register, or when it is named for a
    certificate -- both are read, neither is typed.
    """
    vaults: list[str] = []
    certs: list[str] = []
    for sid in subscription_ids:
        for v in _get_all(f"{ARM}/subscriptions/{sid}/resources"
                          f"?api-version=2021-04-01&$filter=resourceType eq "
                          f"'Microsoft.KeyVault/vaults'", token):
            name = v.get("name", "")
            if name and name not in vaults:
                vaults.append(name)
    known = {v[0] for v in APPROVED.values() if v[0]}
    for sid in subscription_ids:
        for row in _get_all(f"{ARM}/subscriptions/{sid}/providers"
                            f"/Microsoft.Authorization/roleAssignments"
                            f"?api-version=2022-04-01", token):
            scope = row["properties"].get("scope", "")
            if "/secrets/" not in scope:
                continue
            secret = scope.split("/secrets/", 1)[1]
            if secret in certs:
                continue
            if secret in known or secret.startswith(("cert-", "key-", "ca-")):
                certs.append(secret)
    return sorted(vaults), sorted(certs)


def _tenant_name(credential) -> str:
    """The tenant's own name, so the report names the directory it read."""
    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
        r = requests.get(f"{GRAPH}/organization?$select=displayName",
                         headers={"Authorization": f"Bearer {gtoken}"}, timeout=60)
        if r.status_code == 200:
            v = r.json().get("value", [])
            if v:
                return v[0].get("displayName", "")
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("tenant name not readable: %s", exc)
    return ""


def _tenant_id() -> str:
    """The tenant this estate is, taken from the credential rather than typed.

    Resolved the same way the credential itself is: inside Automation the value
    arrives as an environment variable, and on a workstation it lives in .env.
    Reading only the environment returned empty there, which silently disabled
    the filter that keeps Microsoft's own service principals out of this report
    -- 127 of them appeared under a heading that says they are logins nobody
    authorised.
    """
    key = "DEVOPSUSER_AZURE_TENANT_ID"
    value = _ch_os.environ.get(key, "").strip()
    if value or _root is None:
        return value
    try:
        from dotenv import dotenv_values
        return (dotenv_values(_root / ".env").get(key) or "").strip()
    except ImportError:
        return ""


def _role_reach(defs: list[dict]) -> dict[str, dict]:
    """What each role actually permits, computed from its own action set.

    The table used to print the role's description. For a built-in role that is
    Microsoft's text and says what the role does. For a custom role it is text
    somebody here wrote, so the most powerful role in the estate was explained
    by its own author -- chatHealthyAgent said "IDE agent that administers the
    subscription" while its actions were * and its dataActions were *, which is
    every resource and every secret in them.

    Each clause below is a statement about actions, so a role is described by
    what it permits whoever holds it, whatever anyone called it.
    """
    out: dict[str, dict] = {}
    for d in defs:
        name = d["properties"]["roleName"]
        acts, data_acts = set(), set()
        for perm in (d["properties"].get("permissions") or []):
            acts.update(a.strip().lower() for a in (perm.get("actions") or []))
            data_acts.update(a.strip().lower() for a in (perm.get("dataActions") or []))
        clauses = []
        if "*" in acts:
            clauses.append("every management action on every resource in scope")
        if "*" in data_acts:
            clauses.append("every data action in scope, which includes reading the "
                           "contents of every key vault secret")
        elif any(a.startswith("microsoft.keyvault/") and a.endswith("/*")
                 for a in data_acts):
            clauses.append("reads the contents of every secret in the key vaults in scope")
        if any(a.startswith("microsoft.authorization/roleassignments/write")
               or a == "microsoft.authorization/*" for a in acts):
            clauses.append("grants and revokes roles, including to itself")
        if any(a.startswith("microsoft.authorization/roledefinitions/write") for a in acts):
            clauses.append("creates and rewrites role definitions")
        if any(a.startswith("microsoft.managedidentity/userassignedidentities/write")
               for a in acts):
            clauses.append("creates identities")
        if not clauses:
            if acts and all(a.endswith("/read") for a in acts):
                clauses.append("reads the resources named below and changes nothing")
            elif acts:
                clauses.append("the actions named below, and nothing else")
            elif data_acts:
                # A role with data actions and no management actions -- Key Vault
                # Secrets User is the example -- produced no sentence at all and
                # printed as a bare list.
                clauses.append("the data actions named below, and nothing else")
        out[name] = {
            "reach": clauses,
            "custom": d["properties"].get("type") == "CustomRole",
            "actions": sorted(acts),
            "data_actions": sorted(data_acts),
            "not_actions": sorted(
                a for perm in (d["properties"].get("permissions") or [])
                for a in (perm.get("notActions") or [])),
        }
    return out


def _subscriptions(token: str, credential) -> list[dict]:
    """Every subscription in the tenant, and whether this run could read it.

    Listing what the reporting identity can see defines the report's scope by
    its own blind spots: a subscription it cannot read does not appear, and
    neither does any grant inside it, so a principal shown holding two roles may
    hold ten. An empty subscription still exists, and a subscription nobody
    here can read still holds whatever it holds.

    The tenant's own subscription list is asked for first, through the same
    directory the rest of the report reads. What the identity can actually
    enumerate is recorded per subscription, so the report states its reach
    rather than assuming it.
    """
    readable = {s["subscriptionId"]: s
                for s in _get_all(f"{ARM}/subscriptions?api-version=2022-12-01", token)}
    known: dict[str, dict] = {}
    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
        r = requests.get(f"{GRAPH}/directory/subscriptions"
                         f"?$select=id,displayName,ocpSubscriptionId",
                         headers={"Authorization": f"Bearer {gtoken}"}, timeout=60)
        if r.status_code == 200:
            for sub in r.json().get("value", []):
                sid = sub.get("ocpSubscriptionId") or sub.get("id")
                if sid:
                    known[sid] = {"name": sub.get("displayName") or sid}
    except Exception:                                           # noqa: BLE001
        # Unreadable is not fatal here: the tenant list is one of two sources
        # and the report states which subscriptions it could read. The caller
        # logs; this function raises only when neither source yielded one.
        pass

    out = []
    for sid, meta in {**known, **{k: {"name": v.get("displayName") or k}
                                 for k, v in readable.items()}}.items():
        out.append({"id": sid, "name": meta["name"],
                    "state": readable.get(sid, {}).get("state", ""),
                    "readable": sid in readable})
    if not out:
        raise ChatHealthyException(
            mode="config_error",
            component="EntitlementReport",
            message="no subscription is visible to the reporting identity and the "
                    "tenant subscription list could not be read; there is nothing "
                    "to report on")
    out.sort(key=lambda x: x["name"].lower())
    return out


def _group_tree(credential) -> tuple[dict[str, list[str]], dict[str, str],
                                    dict[str, list[str]], bool]:
    """The directory's own classification of every principal.

    The groups are the structure this report stands on. `humans` and `agents`
    say what kind of thing holds a grant, and Entra is the only place that
    fact is recorded by a person rather than asserted by a file -- putting a
    principal in a group is a deliberate act, and this reads that act rather
    than a list somebody typed.

    Returns (membership, description, readable). `membership` maps a principal
    object id to every group it belongs to, transitively, so a grant made to
    `agents` reaches a member of `runtimeAgents` exactly as Azure resolves it.
    `readable` is False when the directory could not be read at all, which the
    caller must surface rather than treat as "no groups exist" -- the two look
    identical in the data and mean opposite things.
    """
    membership: dict[str, list[str]] = {}
    described: dict[str, str] = {}
    try:
        gtoken = credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("group tree not readable: %s", exc)
        return membership, described, {}, False

    headers = {"Authorization": f"Bearer {gtoken}"}
    r = requests.get(f"{GRAPH}/groups?$select=id,displayName,description",
                     headers=headers, timeout=60)
    if r.status_code != 200:
        _LOG.info("group enumeration returned %s", r.status_code)
        return membership, described, {}, False

    groups = r.json().get("value", [])
    by_id = {g["id"]: g["displayName"] for g in groups}

    def direct(group_id: str) -> tuple[list[str], list[str]]:
        """(principal ids, nested group ids) directly in this group.

        Two queries, and the second is not optional. Graph omits service
        principals from the untyped member collection entirely, and its
        transitiveMembers collection omits them whether cast or not -- measured,
        not assumed. Reading only the obvious endpoint returns users and groups
        and reports every agent in the estate as belonging to nothing, which
        reads as a catastrophic finding and is an artefact of the query.
        """
        principals: list[str] = []
        nested: list[str] = []
        for collection in ("members", "members/microsoft.graph.servicePrincipal"):
            page = f"{GRAPH}/groups/{group_id}/{collection}?$select=id,displayName"
            while page:
                m = requests.get(page, headers=headers, timeout=60)
                if m.status_code != 200:
                    _LOG.info("members of %s (%s) returned %s",
                              by_id.get(group_id, group_id), collection, m.status_code)
                    break
                payload = m.json()
                for member in payload.get("value", []):
                    (nested if member["id"] in by_id else principals).append(member["id"])
                page = payload.get("@odata.nextLink") or ""
        return principals, nested

    for group in groups:
        described[group["displayName"]] = group.get("description") or ""

    # Nesting is resolved here rather than by Graph, because the collection that
    # would resolve it does not return service principals. A grant to `agents`
    # reaches a member of `runtimeAgents`, so the walk records both names
    # against that member, exactly as Azure resolves the grant.
    for group in groups:
        seen_groups: set[str] = set()
        frontier = [group["id"]]
        while frontier:
            gid = frontier.pop()
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
            principals, nested = direct(gid)
            for pid in principals:
                names = membership.setdefault(pid, [])
                if group["displayName"] not in names:
                    names.append(group["displayName"])
            frontier.extend(nested)
    group_owners = {}
    for group in groups:
        names = []
        for cast in ("microsoft.graph.user", "microsoft.graph.servicePrincipal"):
            o = requests.get(f"{GRAPH}/groups/{group['id']}/owners/{cast}?$select=displayName",
                             headers=headers, timeout=60)
            if o.status_code == 200:
                names.extend(x.get("displayName", "") for x in o.json().get("value", []))
        group_owners[group["displayName"]] = sorted(n for n in names if n)
    return membership, described, group_owners, True


def collect() -> dict:
    credential = _credential()
    token = credential.get_token(f"{ARM}/.default").token
    group_membership, group_descriptions, group_owners, groups_readable = _group_tree(credential)

    subscriptions = _subscriptions(token, credential)

    # Every subscription the reporting identity can see is walked. Aggregating
    # rather than picking one is the point: a grant in a subscription this report
    # skipped is indistinguishable, on the page, from a grant that does not
    # exist.
    roles: dict[str, str] = {}
    role_text: dict[str, str] = {}
    privileged_roles: set[str] = set()
    all_defs: list[dict] = []
    rows: list[dict] = []
    existing: dict[str, dict] = {}
    for sub in subscriptions:
        if not sub["readable"]:
            continue
        sid = sub["id"]
        defs = _get_all(f"{ARM}/subscriptions/{sid}/providers"
                        f"/Microsoft.Authorization/roleDefinitions?api-version=2022-04-01", token)
        all_defs.extend(defs)
        for d in defs:
            roles[d["id"].rsplit("/", 1)[-1]] = d["properties"]["roleName"]
            role_text[d["properties"]["roleName"]] = (
                d["properties"].get("description") or "").strip()
            # Privilege is read off each role's published actions, so a renamed
            # role, or one authored tomorrow, is judged on what it permits rather
            # than on whether somebody remembered to add its name to this file.
            for perm in (d["properties"].get("permissions") or []):
                for action in (perm.get("actions") or []):
                    if action.strip().lower() in PRIVILEGE_MARKERS:
                        privileged_roles.add(d["properties"]["roleName"])
                # A data-plane wildcard is administrative in the sense that
                # matters here: Key Vault Administrator names no privileged verb
                # and yet reads every certificate in the firm. The same test is
                # NOT applied to control-plane actions, where a service wildcard
                # like Microsoft.Network/* means Contributor over networking and
                # confers no authority over who holds rights.
                for action in (perm.get("dataActions") or []):
                    a = action.strip().lower()
                    if a in PRIVILEGE_MARKERS or a.endswith("/*"):
                        privileged_roles.add(d["properties"]["roleName"])
        for row in _get_all(f"{ARM}/subscriptions/{sid}/providers"
                            f"/Microsoft.Authorization/roleAssignments"
                            f"?api-version=2022-04-01", token):
            row["_subscription"] = sub["name"]
            rows.append(row)
        for pid, meta in _all_principals(token, credential, sid).items():
            existing.setdefault(pid, meta)
    oids = sorted({r["properties"]["principalId"] for r in rows})
    # ARM first -- managed identities resolve without any directory permission.
    # Graph then fills in users and app registrations, when permitted.
    names = _managed_identity_names(token, [s['id'] for s in subscriptions])
    graph_names = _principal_names(oids, credential)
    names.update(graph_names)

    sub_ids = tuple(s["id"] for s in subscriptions)
    role_contains = _role_containment(all_defs)
    role_reach = _role_reach(all_defs)
    vaults, certificate_secrets = _vaults_and_certificates(token, list(sub_ids))

    # Resource groups, stated once against the subscription they belong to. A
    # resource group lives in exactly one subscription, so once that is said an
    # entitlement need only name the resource group and what is inside it.
    resource_groups: list[dict] = []
    for sub in subscriptions:
        if not sub["readable"]:
            continue
        for rg in _get_all(f"{ARM}/subscriptions/{sub['id']}/resourcegroups"
                           f"?api-version=2021-04-01", token):
            resource_groups.append({"name": rg.get("name", ""),
                                    "subscription": sub["name"],
                                    "location": rg.get("location", "")})
    resource_groups.sort(key=lambda r: (r["subscription"].lower(), r["name"].lower()))

    # Every resource, the group that owns it, and its description tag. A resource
    # nobody described is a resource nobody can account for, so it is reported.
    resources: list[dict] = []

    # A resource's own description, which the person who created it controls.
    # Azure carries it as a tag; nothing else on a resource is free text.
    resource_notes: dict[str, str] = {}
    for sub in subscriptions:
        if not sub["readable"]:
            continue
        for r in _get_all(f"{ARM}/subscriptions/{sub['id']}/resources"
                          f"?api-version=2021-04-01", token):
            tags = r.get("tags") or {}
            note = ""
            for key in ("description", "Description", "purpose", "Purpose"):
                if tags.get(key):
                    note = tags[key]
                    break
            if note and r.get("id"):
                resource_notes[r["id"]] = note
            rid = r.get("id", "")
            group = ""
            low = rid.lower()
            if "/resourcegroups/" in low:
                group = rid[low.index("/resourcegroups/")
                            + len("/resourcegroups/"):].split("/", 1)[0]
            resources.append({
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "group": group,
                "subscription": sub["name"],
                "description": note,
            })

    # What a subscription actually contains. "Owner on subscription X" tells a
    # reader nothing about what can be touched; the count of resources, and the
    # vaults among them, is the thing a security officer is reading for.
    sub_contents: dict[str, dict] = {}
    for sub in subscriptions:
        if not sub["readable"]:
            continue
        res = _get_all(f"{ARM}/subscriptions/{sub['id']}/resources"
                       f"?api-version=2021-04-01", token)
        kinds: dict[str, int] = {}
        for r in res:
            kinds[r.get("type", "")] = kinds.get(r.get("type", ""), 0) + 1
        sub_contents[sub["name"]] = {
            "count": len(res),
            "vaults": kinds.get("Microsoft.KeyVault/vaults", 0),
            "kinds": kinds,
        }
    holders: dict[str, dict] = {}
    for r in rows:
        p = r["properties"]
        oid = p["principalId"]
        approved = APPROVED.get(oid)
        known = names.get(oid)
        entry = holders.setdefault(oid, {
            "object_id": oid,
            "name": approved[0] if approved else (known["name"] if known else ""),
            "purpose": approved[1] if approved else "",
            # Group membership is the directory's own answer to "what kind of
            # thing is this" -- a person put it there. actor_type from the
            # register is the second opinion, and the two disagreeing is
            # itself worth seeing.
            "groups": sorted(group_membership.get(oid, [])),
            "actor_type": approved[2] if approved else "",
            "entra_object_type": (approved[3] if approved else
                                  (known["kind"] if known
                                   else p.get("principalType", ""))),
            "application": approved[4] if approved else "",
            "declared_roles": list(approved[5]) if approved else [],
            "type": known["kind"] if known else p.get("principalType", "Unknown"),
            "approved": approved is not None,
            "resolvable": known is not None,
            "record": (known or {}).get("record", "none"),
            "qualities": (known or {}).get("qualities", {}),
            # A principal that does not resolve in the directory has been
            # deleted; its assignment outlived it. That is not "outside the
            # approved register" -- no register can contain a deleted object,
            # and listing it as one invites someone to add it rather than
            # remove the grant.
            "orphaned": known is None and approved is None,
            "grants": [],
        })
        role_name = roles.get(p["roleDefinitionId"].rsplit("/", 1)[-1],
                              p["roleDefinitionId"].rsplit("/", 1)[-1])
        condition = p.get("condition") or ""
        # Name the roles the condition forbids, by testing which role-definition
        # ids the expression mentions. An auditor needs the restriction stated,
        # not merely flagged.
        forbidden = sorted({name for guid, name in roles.items() if guid in condition})
        entry["grants"].append({
            "role": role_name,
            "scope": _scope_label(p.get("scope", ""), sub_ids),
            "raw_scope": p.get("scope", ""),
            "secret": (p.get("scope", "").split("/secrets/", 1)[1]
                       if "/secrets/" in p.get("scope", "") else ""),
            "parent_scope": _scope_label(p.get("scope", "").split("/secrets/", 1)[0], sub_ids)
                            if "/secrets/" in p.get("scope", "") else "",
            "redundant": "",
            "privileged": role_name in privileged_roles,
            "conditioned": bool(condition),
            "forbidden_roles": forbidden,
            "constrains_write": "roleAssignments/write" in condition,
            "constrains_delete": "roleAssignments/delete" in condition,
            "justification": (p.get("description") or "").strip(),
            "subscription": r.get("_subscription", ""),
        })

    for h in holders.values():
        _mark_redundant(h["grants"], sub_ids, role_contains)
        h["grants"].sort(key=lambda g: (not g["privileged"], g["scope"], g["role"]))
        h["privileged_count"] = sum(1 for g in h["grants"] if g["privileged"])

    holders_list = sorted(
        holders.values(),
        key=lambda h: (h["approved"], -h["privileged_count"], h["name"] or h["object_id"]))

    # Principals that exist and hold nothing. A report built from assignments
    # alone cannot see these, and a principal minted quietly lands here.
    rightless = []
    for oid, meta in sorted(existing.items(), key=lambda kv: kv[1]["name"].lower()):
        if oid in holders:
            continue
        approved = APPROVED.get(oid)
        # A principal holding nothing is an exception whether or not the
        # register names it. Being declared does not make an orphan expected:
        # something brought it into existence and nothing uses it, and that is
        # the fact a reviewer must see. It is listed unapproved, with a note
        # saying what is true of it.
        rightless.append({
            "object_id": oid,
            "name": approved[0] if approved else meta["name"],
            "type": meta["kind"],
            "actor_type": approved[2] if approved else "",
            "application": approved[4] if approved else "",
            "origin": meta["origin"],
            "approved": False,
            "note": ("holds no rights; named in the approved register"
                     if approved is not None
                     else "holds no rights; not in the approved register"),
        })

    missing = [v[0] for k, v in APPROVED.items() if k not in holders]

    # Who answers for each principal and each group. Delegation is a fact about
    # authority as much as any role assignment, and it lives only here.
    owner_targets = {h["object_id"]: "servicePrincipals"
                     for h in holders_list if h["type"].lower() != "user"}
    owner_targets.update({h["object_id"]: "users"
                          for h in holders_list if h["type"].lower() == "user"})
    owners = _owners(credential, owner_targets)

    # Conditions are a property of the right, not of the identity holding it, so
    # they are gathered per role and stated once in the rights table. Repeated
    # under every holder they crowded out that holder's actual entitlements.
    role_conditions: dict[str, list[str]] = {}
    for h in holders_list:
        for g in h["grants"]:
            if g["conditioned"] and g["forbidden_roles"]:
                role_conditions.setdefault(g["role"], [])
                for f in g["forbidden_roles"]:
                    if f not in role_conditions[g["role"]]:
                        role_conditions[g["role"]].append(f)

    # Ownership, derived from scope and actions rather than from a role name.
    # A principal owns a subscription when it holds, at that subscription's own
    # scope, a role permitting every management action. It owns the tenant when
    # it holds such a role at the tenant root, which sits above every
    # subscription the tenant will ever contain. Neither is read from a role
    # name or a description: claudeCodeAgent is a subscription owner through a
    # role called chatHealthyAgent, and nothing but its actions says so.
    def _total(role: str) -> bool:
        meta = role_reach.get(role, {})
        return "*" in meta.get("actions", [])

    directory_roles = _directory_roles(credential)
    # A directory role naming the whole directory is tenant authority. Global
    # Administrator is the one Microsoft ships for it; any role granting the
    # same is caught by the same test rather than by its name.
    TENANT_ROLES = {"Global Administrator", "Company Administrator",
                    "Privileged Role Administrator"}
    for h in holders_list:
        h["directory_roles"] = sorted(directory_roles.get(h["object_id"], []))
        owns_subs = []
        owns_tenant = bool(set(h["directory_roles"]) & TENANT_ROLES)
        for g in h["grants"]:
            if not _total(g["role"]):
                continue
            raw = g["raw_scope"].rstrip("/")
            if raw == "":
                owns_tenant = True
            for sub in subscriptions:
                if raw.lower() == f"/subscriptions/{sub['id']}".lower():
                    if sub["name"] not in owns_subs:
                        owns_subs.append(sub["name"])
        h["owns_subscriptions"] = owns_subs
        h["owns_tenant"] = owns_tenant

        # For an owner, control-plane grants are implied by the ownership and
        # say nothing. Data-plane grants are NOT: Owner permits managing a key
        # vault and not reading a secret in it, so a data grant to an owner is
        # an addition to what ownership gives and has to be stated.
        for g in h["grants"]:
            in_owned = any(f"/subscriptions/{sub['id']}".lower()
                           in g["raw_scope"].lower()
                           for sub in subscriptions
                           if sub["name"] in owns_subs)
            has_data = bool(role_reach.get(g["role"], {}).get("data_actions"))
            g["beyond_ownership"] = bool(in_owned and has_data)
            g["implied_by_ownership"] = bool(
                in_owned and not has_data
                and g["role"] not in ("Owner",))

    # Two different facts were being reported as one. A principal holding a
    # vault-wide role can read every secret in that vault, which made every
    # secret look shared by everyone and buried the secrets that are actually
    # shared. Vault-wide access is stated once, as its own list. A secret is
    # shared only when more than one principal is granted that secret by name.
    vault_wide: list[dict] = []
    for h in holders_list:
        for g in h["grants"]:
            data_acts = role_reach.get(g["role"], {}).get("data_actions", [])
            reaches_secrets = any(a == "*" or a.startswith("microsoft.keyvault/vaults/")
                                  for a in data_acts)
            if not reaches_secrets or g["secret"]:
                continue
            writes = any(a == "*" or a.rstrip("/*").endswith("microsoft.keyvault/vaults")
                         or "setsecret" in a or "/secrets/*" in a for a in data_acts)
            vault_wide.append({
                "principal": h["name"] or h["object_id"],
                "role": g["role"],
                "where": _reaches(g["raw_scope"], g["subscription"], {}),
                "access": "read and write" if writes else "read",
            })
    vault_wide.sort(key=lambda x: (x["principal"].lower(), x["role"].lower()))

    named: dict[str, set[str]] = {}
    for h in holders_list:
        for g in h["grants"]:
            if g["secret"]:
                named.setdefault(g["secret"], set()).add(h["name"] or h["object_id"])
    shared = sorted(((secret, sorted(who)) for secret, who in named.items()
                     if len(who) > 1), key=lambda x: (-len(x[1]), x[0]))

    redundant = [(h["name"] or h["object_id"], g["role"], g["scope"], g["redundant"])
                 for h in holders_list for g in h["grants"] if g["redundant"]]

    # A principal holding rights while belonging to no group is the finding this
    # report exists for: nobody placed it, so nobody classified it, so nothing
    # states whether a person or a program holds what it holds.
    ungrouped = [h["name"] or h["object_id"]
                 for h in holders_list if not h["groups"]] if groups_readable else []

    return {
        "generated": _dt.datetime.now(_dt.timezone.utc),
        "holders": holders_list,
        "assignment_count": len(rows),
        "names_resolved": bool(graph_names),
        "secret_descriptions": _secret_descriptions(),
        "rightless": rightless,
        "directory_enumerated": any(m["origin"] == "directory" for m in existing.values()),
        "subscriptions": subscriptions,
        "privileged_roles": sorted(privileged_roles),
        "role_reach": role_reach,
        "role_conditions": {k: sorted(v) for k, v in role_conditions.items()},
        "resource_groups": resource_groups,
        "subscription_contents": sub_contents,
        "resource_notes": resource_notes,
        "resources": resources,
        "undescribed": [r for r in resources if not r["description"]],
        "vaults": vaults,
        "certificate_secrets": certificate_secrets,
        "tenant_name": _tenant_name(credential) or _tenant_id(),
        "shared_secrets": shared,
        "vault_wide": vault_wide,
        "redundant_grants": redundant,
        "owners": owners,
        "group_owners": group_owners,
        "groups_readable": groups_readable,
        "group_descriptions": group_descriptions,
        "ungrouped": ungrouped,
        "role_text": {r: t for r, t in role_text.items()
                      if any(g["role"] == r for h in holders_list for g in h["grants"])},
        "approved_absent": missing,
    }


class _NumberedCanvas(canvas_module.Canvas):
    """Holds every page until the end so each can be stamped "n of m".

    A page numbered without its total cannot tell a reader whether the report
    in front of them is complete. The total is only known once the last page
    exists, so pages are kept and stamped on save.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            w, h = landscape(letter)
            self.drawRightString(w - 0.5 * inch, h - 0.52 * inch,
                                 f"{self._pageNumber}/{total}")
            super().showPage()
        super().save()


def _page_furniture(canvas, doc):
    canvas.saveState()
    w, h = landscape(letter)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, h - 0.62 * inch, w - 0.5 * inch, h - 0.62 * inch)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.5 * inch, h - 0.52 * inch,
                      "ChatHealthy.ai  |  Access entitlement report  |  Confidential  |  "
                      + getattr(doc, "ch_stamp", "")
                      + "  |  register: " + getattr(doc, "ch_register", ""))
    # Page n of m. The total is known only after the first pass, so the document
    # is built twice and the count carried between them; a page numbered without
    # its total cannot tell a reader whether the report is complete.
    canvas.line(0.5 * inch, 0.55 * inch, w - 0.5 * inch, 0.55 * inch)
    canvas.drawString(0.5 * inch, 0.4 * inch,
                      getattr(doc, "ch_scope_line", "Azure subscriptions: unknown"))
    canvas.restoreState()


def render_pdf(data: dict, out_path: Path) -> Path:
    base = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=base["Heading1"], fontSize=19, leading=23,
                           textColor=INK, spaceAfter=2)
    sub = ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, textColor=MUTED,
                         spaceAfter=14)
    SECTION_NUMBER = colors.HexColor("#1F5FBF")
    sec = ParagraphStyle("sec", parent=base["Heading2"], fontSize=12.5, textColor=INK,
                         spaceBefore=16, spaceAfter=6)
    sub_sec = ParagraphStyle("subsec", parent=base["Heading3"], fontSize=10.5,
                             textColor=INK, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("b", parent=base["Normal"], fontSize=9, leading=13,
                          textColor=INK, spaceAfter=8)
    note = ParagraphStyle("n", parent=base["Normal"], fontSize=7.5, leading=10,
                          textColor=MUTED)
    who = ParagraphStyle("w", parent=base["Heading3"], fontSize=10.5, textColor=INK,
                         spaceBefore=10, spaceAfter=1)
    cell = ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10.5)
    global _SECTION_TITLE, _SECTION_NOTE
    _SECTION_TITLE = ParagraphStyle(
        "sectitle", parent=base["Heading2"], fontSize=17, leading=21,
        textColor=colors.HexColor("#1F5FBF"), spaceBefore=16, spaceAfter=1)
    _SECTION_NOTE = ParagraphStyle(
        "secnote", parent=base["Normal"], fontSize=9.5, leading=13,
        leftIndent=16, fontName="Helvetica-Oblique", textColor=INK,
        spaceAfter=0)
    bullet = ParagraphStyle("bul", parent=base["Normal"], fontSize=9, leading=13,
                            leftIndent=22, spaceAfter=2)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.8 * inch, bottomMargin=0.75 * inch,
        title="ChatHealthy access entitlement report",
        author="ChatHealthy.ai", subject="Access entitlement review")
    doc.ch_scope_line = "Azure subscriptions: " + ", ".join(
        s["name"] for s in data["subscriptions"])

    # Stated in the operator's own time. A report read every morning in
    # California should not make its reader convert from UTC to know whether
    # it is this morning's.
    try:
        from zoneinfo import ZoneInfo
        local = data["generated"].astimezone(ZoneInfo("America/Los_Angeles"))
        stamp = local.strftime("%d %B %Y at %H:%M %Z")
    except Exception:                                           # noqa: BLE001
        stamp = data["generated"].strftime("%d %B %Y at %H:%M UTC")
    doc.ch_stamp = stamp
    doc.ch_register = _REGISTER_SOURCE
    # A principal holding nothing belongs here too. unapproved read only from
    # holders - identities that hold rights - so an orphan could not appear in
    # it by construction, and the section that exists to surface exceptions
    # reported none while orphans existed.
    unapproved = [h for h in data["holders"]
                  if not h["approved"] and not h.get("orphaned")]
    unapproved = unapproved + [{
        "object_id": r["object_id"],
        "name": r["name"],
        "purpose": r.get("note", ""),
        "purpose_source": "Observed by this run",
        "groups": [],
        "actor_type": r.get("actor_type", ""),
        "entra_object_type": r["type"],
        "application": r.get("application", ""),
        "declared_roles": [],
        "type": r["type"],
        "approved": False,
        "resolvable": True,
        "record": "none",
        "qualities": {},
        "orphaned": False,
        "grants": [],
        "privileged_count": 0,
        "owns_tenant": False,
        "owns_subscriptions": [],
        "managers": [],
    } for r in data["rightless"] if not r["approved"]]

    orphaned = [h for h in data["holders"] if h.get("orphaned")]
    undeclared = [r for r in data["rightless"] if not r["approved"]]
    approved = [h for h in data["holders"] if h["approved"]]
    priv_unapproved = [h for h in unapproved if h["privileged_count"]]

    story: list = []
    story.append(Paragraph("Access entitlement report", title))
    story.append(Paragraph(
        "Azure subscriptions &nbsp;&middot;&nbsp; "
        + " &nbsp;|&nbsp; ".join(s["name"] for s in data["subscriptions"])
        + f"<br/>Enumerated {stamp}", sub))

    story.append(Paragraph(
        "This is the operational report of who can act on ChatHealthy systems. Every value "
        "in it is read at the time stated, from Azure and from the directory. It states no "
        "fact of its own. Its sections are:", body))
    for number, (label, line) in enumerate(SECTIONS, start=1):
        story.append(Paragraph(
            f'<font size="15" color="#1F5FBF"><b>{number}</b></font>&nbsp;&nbsp;'
            f"<b>{label}</b> &mdash; {line}", bullet))
    story.append(Spacer(1, 6))

    def _grant_table(holder: dict) -> Table:
        # Grants that reach a single secret are gathered under one line naming
        # the vault, with the secrets themselves listed beneath it. Otherwise a
        # role held on six secrets reads as six near-identical rows whose only
        # difference sits at the end of a long string.
        descs = data["secret_descriptions"]
        # user -> resource -> right. The resource leads, because that is the
        # thing being protected; the right is how this user touches it.
        rows = [["Resource", "What it holds", "Description", "Right held on it",
                 "Administrative", "Conditioned", "Adds nothing"]]
        bullet_rows: list[int] = []
        title_rows: list[int] = []
        # Every entitlement is titled with the subscription it is granted in.
        # Without it a scope reads "the whole subscription" with no way to know
        # which, and two grants in different subscriptions look identical.
        by_sub: dict[str, list[dict]] = {}
        for g in holder["grants"]:
            by_sub.setdefault(g["subscription"] or "(subscription not recorded)",
                              []).append(g)

        for sub_name in sorted(by_sub):
            title_rows.append(len(rows))
            rows.append([Paragraph(f'<b>{sub_name}</b>', cell),
                         "", "", "", "", "", ""])
            # One row per grant. Two grants on the same resource are two facts,
            # and folding them into one row because the resource repeats hides
            # one of them.
            for g in by_sub[sub_name]:
                label = _reaches(g["raw_scope"], g["subscription"],
                                 data["subscription_contents"])
                holds = _holds(g["raw_scope"], g["subscription"],
                               data["subscription_contents"])
                described = data["resource_notes"].get(g["raw_scope"], "")
                if g["secret"]:
                    label = f'secret {g["secret"]}'
                    holds = ""
                    described = descs.get(g["secret"], "")
                rows.append([
                    Paragraph(label, cell),
                    Paragraph(holds, cell),
                    Paragraph(described, cell),
                    Paragraph(g["role"], cell),
                    "yes" if g["privileged"] else "",
                    "yes" if g["conditioned"] else "",
                    "yes" if g["redundant"] else "",
                ])

        tb = Table(rows, colWidths=[2.5 * inch, 1.1 * inch, 2.1 * inch,
                                    1.6 * inch, 0.9 * inch, 0.85 * inch,
                                    0.85 * inch],
                   hAlign="LEFT", repeatRows=1)
        st = [
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (4, 0), (6, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for i in bullet_rows:
            st.append(("LINEBELOW", (0, i), (-1, i), 0, colors.white))
            st.append(("TOPPADDING", (0, i), (-1, i), 0))
        for i, row in enumerate(rows[1:], start=1):
            if i not in bullet_rows and row[4] == "yes":
                st.append(("TEXTCOLOR", (4, i), (4, i), FLAG))
                st.append(("FONTNAME", (0, i), (0, i), "Helvetica-Bold"))
        tb.setStyle(TableStyle(st))
        return tb

    def _block(holder: dict) -> list:
        label = holder["name"] or "Unidentified principal"
        out = [Paragraph(label, who)]
        meta = holder["entra_object_type"] or holder["type"]
        if holder["owns_tenant"]:
            meta += " &nbsp;&middot;&nbsp; tenant owner"
        if holder["owns_subscriptions"]:
            meta += (" &nbsp;&middot;&nbsp; owns "
                     + ", ".join(holder["owns_subscriptions"]))
        if holder["groups"]:
            meta += " &nbsp;&middot;&nbsp; " + ", ".join(holder["groups"])
        out.append(Paragraph(meta, note))
        # Who answers for this identity existing. An entitlement table says what
        # an identity may do and never who is accountable for it, and in an
        # estate where a person empowers an agent that then administers others,
        # that chain is the control.
        held_by = data["owners"].get(holder["object_id"], [])
        if held_by:
            out.append(Paragraph(f"Managed by {', '.join(held_by)}.", note))
        elif holder["type"].lower() != "user" and holder["entra_object_type"] != "managedIdentity":
            out.append(Paragraph("No owner recorded in the directory.", note))
        # Cited as the catalog's description rather than stated as fact. It is
        # prose someone wrote and it goes stale: the operator's said he was the
        # only identity able to create another, which stopped being true the
        # hour an agent was granted Owner and Application.ReadWrite.All.
        if holder["purpose"]:
            out.append(Paragraph(
                f"<i>" + (holder.get("purpose_source")
                          or "Description, from the identity catalog")
                + f":</i> {holder['purpose']}", note))
        if holder["record"] == "deleted":
            q = ", ".join(f"{k} {v}" for k, v in holder["qualities"].items() if v)
            out.append(Paragraph(
                f"<b>The grant below is held by a deleted account.</b> {q}.", note))
        elif holder["record"] == "none":
            out.append(Paragraph(
                "<b>The directory holds no record of this holder</b> &mdash; not among "
                "its objects and not among its deleted ones. Nothing can be restored, "
                "because there is nothing recorded to restore.", note)
                if data["directory_enumerated"] else Paragraph(
                "The directory could not be read on this run, so this holder was "
                "neither confirmed nor ruled out.", note))

        out.append(Spacer(1, 4))
        if holder["grants"]:
            out.append(_grant_table(holder))
        out.append(Spacer(1, 10))
        return out

    story.extend(_section_block(1))
    scope_rows = [["", ""]]
    scope_rows.append(["Tenant", Paragraph(data["tenant_name"], cell)])
    for sub in data["subscriptions"]:
        scope_rows.append([
            Paragraph("Subscription", cell),
            Paragraph(f"<b>{sub['name']}</b> &mdash; "
                      + ("enumerated in full" if sub["readable"] else
                         "NOT ENUMERATED: the reporting identity holds no access here, "
                         "so grants inside it are absent from this report"), cell)])
    scope_rows.append(["Certificate secrets",
                       Paragraph(f"{len(data['certificate_secrets'])} in "
                                 + (", ".join(data["vaults"]) or "no vault visible"), cell)])
    scope_rows.append(["Directory", Paragraph(
        "groups and ownership read" if data["groups_readable"]
        else "NOT READ: nothing below is classified", cell)])
    scope_rows.append(["Covers", Paragraph(
        "every identity holding rights in the subscriptions marked enumerated, "
        "what each may do, how it is classified and who manages it", cell)])
    st = Table(scope_rows[1:], colWidths=[2.0 * inch, 7.4 * inch], hAlign="LEFT")
    st.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(st)

    story.extend(_section_block(2))
    summary = [
        ["Role assignments in force", str(data["assignment_count"])],
        ["Principals holding rights", str(len(data["holders"]))],
        ["Approved identities present", f"{len(approved)} of {len(APPROVED)}"],
        ["Orphaned assignments", str(len(orphaned))],
        ["Resources undescribed", str(len(data["undescribed"]))],
        ["Exceptions in total", str(len(orphaned) + len(data["undescribed"]))],
    ]
    t = Table(summary, colWidths=[4.2 * inch, 1.2 * inch], hAlign="LEFT")
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if unapproved:
        style.append(("TEXTCOLOR", (1, 3), (1, 4), FLAG))
        style.append(("FONTNAME", (1, 3), (1, 4), "Helvetica-Bold"))
    else:
        style.append(("TEXTCOLOR", (1, 3), (1, 3), OK))
    t.setStyle(TableStyle(style))
    story.append(KeepTogether(t))

    if data["approved_absent"]:
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "Approved identities holding no rights in this subscription: "
            + ", ".join(data["approved_absent"]) + ".", note))
    if not data["names_resolved"]:
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "Directory names were unavailable when this report ran: the reporting identity "
            "holds no Microsoft Graph directory-read permission. Approved identities are "
            "named from the firm's pinned register; all others are identified by object id "
            "alone. Granting Directory.Read.All to the reporting identity resolves this.",
            note))

    # The header travels with the first principal. A page break can be told
    # how much room to require, but not how tall the next block will be, so
    # binding the two is what actually keeps a title off the foot of a page.
    head = _section_block(3, str(len(approved)) + " found")
    if approved:
        story.append(KeepTogether(head[1:] + _block(approved[0])))
        for h in approved[1:]:
            story.append(KeepTogether(_block(h)))
    else:
        story.extend(head)

    story.extend(_section_block(4))
    # The section name repeats with the column header. Without it a table that
    # runs over three pages reads as three sections, when it is one list sorted
    # alphabetically.
    gl = [[Paragraph("<b>What each right permits</b> &nbsp;&middot;&nbsp; "
                     "continued, one list in alphabetical order", cell), "", ""],
          ["Right", "Administrative", "What it permits"]]
    for role in sorted(data["role_text"], key=str.lower):
        meta = data["role_reach"].get(role, {})
        parts = []
        if meta.get("reach"):
            parts.append("<b>Permits " + "; ".join(meta["reach"]) + ".</b>")
        if meta.get("not_actions"):
            parts.append("<b>Except:</b> " + ", ".join(meta["not_actions"]) + ".")
        acts = meta.get("actions", [])
        data_acts = meta.get("data_actions", [])
        if acts:
            shown = acts[:6]
            more = f" and {len(acts) - len(shown)} more" if len(acts) > len(shown) else ""
            parts.append("<font size=7>actions: " + ", ".join(shown) + more + "</font>")
        if data_acts:
            shown = data_acts[:4]
            more = (f" and {len(data_acts) - len(shown)} more"
                    if len(data_acts) > len(shown) else "")
            parts.append("<font size=7>data actions: " + ", ".join(shown) + more + "</font>")
        # Cited, with its author named, never stated as the report's own finding.
        described = (data["role_text"].get(role) or "").strip()
        if described:
            source = ("the ChatHealthy role definition" if meta.get("custom")
                      else "the Azure role definition")
            parts.append(f"<font size=7><i>Description, from {source}:</i> "
                         f"{described}</font>")
        forbidden = data["role_conditions"].get(role)
        if forbidden:
            parts.append("<b>Conditioned where held:</b> the holder may neither grant "
                         "nor revoke " + ", ".join(forbidden)
                         + ", to any principal including itself. The list includes this "
                           "role, so the holder cannot lift the condition from its own "
                           "assignment.")
        gl.append([Paragraph(role, cell),
                   "yes" if role in data["privileged_roles"] else "",
                   Paragraph(" ".join(parts), cell)])
    gt = Table(gl, colWidths=[2.4 * inch, 1.0 * inch, 5.95 * inch], hAlign="LEFT",
               repeatRows=2)
    gst = [
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 1), BAND),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, role in enumerate(sorted(data["role_text"], key=str.lower), start=2):
        if role in data["privileged_roles"]:
            gst.append(("TEXTCOLOR", (1, i), (1, i), FLAG))
    gt.setStyle(TableStyle(gst))
    story.append(gt)

    story.extend(_section_block(5, str(len(data["resource_groups"])) + " found"))
    if data["resource_groups"]:
        rg_rows = [["Resource group", "Subscription", "Region"]]
        for rg in data["resource_groups"]:
            rg_rows.append([Paragraph(f"<b>{rg['name']}</b>", cell),
                            Paragraph(rg["subscription"], cell),
                            Paragraph(rg["location"], cell)])
        rgt = Table(rg_rows, colWidths=[3.4 * inch, 3.6 * inch, 2.4 * inch],
                    hAlign="LEFT", repeatRows=1)
        rgt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(rgt)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    story.append(PageBreak())

    story.extend(_section_block(6))
    story.append(Paragraph(
        f"Orphaned assignments &mdash; grants whose principal no longer exists "
        f"({len(orphaned)})", sub_sec))
    if orphaned:
        o_rows = [["Principal object id", "Right", "Resource", "Subscription"]]
        for h in orphaned:
            for g in h["grants"]:
                o_rows.append([
                    Paragraph(h["object_id"], cell),
                    Paragraph(g["role"], cell),
                    Paragraph(_reaches(g["raw_scope"], g["subscription"],
                                       data["subscription_contents"]), cell),
                    Paragraph(g["subscription"], cell)])
        ot = Table(o_rows, colWidths=[2.6 * inch, 1.9 * inch, 3.0 * inch, 1.9 * inch],
                   hAlign="LEFT", repeatRows=1)
        ot.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ot)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    story.append(Paragraph(
        f"Resources undescribed &mdash; carrying no description tag "
        f"({len(data['undescribed'])})", sub_sec))
    if data["undescribed"]:
        u_rows = [["Resource", "Resource group", "Subscription", "Type"]]
        for r in sorted(data["undescribed"],
                        key=lambda x: (x["subscription"].lower(), x["name"].lower())):
            u_rows.append([Paragraph(r["name"], cell),
                           Paragraph(r["group"] or "&mdash;", cell),
                           Paragraph(r["subscription"], cell),
                           Paragraph(r["type"], cell)])
        ut = Table(u_rows, colWidths=[3.0 * inch, 2.2 * inch, 2.2 * inch, 2.0 * inch],
                   hAlign="LEFT", repeatRows=1)
        ut.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ut)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))


    story.append(Paragraph(
        "Shared secrets &mdash; granted by name to more than one principal ("
        + str(len(data["shared_secrets"])) + ")", sub_sec))
    if data["shared_secrets"]:
        rows = [["Secret", "Granted by name to"]]
        for secret, names in data["shared_secrets"]:
            rows.append([Paragraph(f"<b>{secret}</b>", cell),
                         Paragraph(", ".join(names), cell)])
        ts = Table(rows, colWidths=[3.2 * inch, 6.2 * inch], hAlign="LEFT", repeatRows=1)
        ts.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ts)
    else:
        story.append(Paragraph("No secrets are shared. No secret is granted by name to "
                               "more than one principal.", body))

    story.append(Paragraph(
        f"Principals outside the approved list ({len(unapproved)})", sub_sec))
    if unapproved:
        for h in unapproved:
            story.append(KeepTogether(_block(h)))
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    story.append(Paragraph(
        f"Orphaned assignments &mdash; grants whose principal no longer exists "
        f"({len(orphaned)})", sub_sec))
    if orphaned:
        o_rows = [["Principal object id", "Right", "Resource", "Subscription"]]
        for h in orphaned:
            for g in h["grants"]:
                o_rows.append([
                    Paragraph(h["object_id"], cell),
                    Paragraph(g["role"], cell),
                    Paragraph(_reaches(g["raw_scope"], g["subscription"],
                                       data["subscription_contents"]), cell),
                    Paragraph(g["subscription"], cell)])
        ot = Table(o_rows, colWidths=[2.6 * inch, 1.9 * inch, 3.0 * inch, 1.9 * inch],
                   hAlign="LEFT", repeatRows=1)
        ot.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ot)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    story.append(Paragraph(
        f"Resources undescribed &mdash; carrying no description tag "
        f"({len(data['undescribed'])})", sub_sec))
    if data["undescribed"]:
        u_rows = [["Resource", "Resource group", "Subscription", "Type"]]
        for r in sorted(data["undescribed"],
                        key=lambda x: (x["subscription"].lower(), x["name"].lower())):
            u_rows.append([Paragraph(r["name"], cell),
                           Paragraph(r["group"] or "&mdash;", cell),
                           Paragraph(r["subscription"], cell),
                           Paragraph(r["type"], cell)])
        ut = Table(u_rows, colWidths=[3.0 * inch, 2.2 * inch, 2.2 * inch, 2.0 * inch],
                   hAlign="LEFT", repeatRows=1)
        ut.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ut)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))


    story.append(Paragraph(
        f"Grants that add nothing &nbsp;&middot;&nbsp; access held for no stated reason "
        f"({len(data['redundant_grants'])})", sub_sec))
    if data["redundant_grants"]:
        rows = [["Principal", "Role", "Scope", "Also covered by"]]
        for holder_name, role, scope, why in data["redundant_grants"]:
            rows.append([Paragraph(holder_name, cell), Paragraph(role, cell),
                         Paragraph(scope, cell), Paragraph(why, cell)])
        tr = Table(rows, colWidths=[1.7 * inch, 1.9 * inch, 2.7 * inch, 3.1 * inch],
                   hAlign="LEFT", repeatRows=1)
        tr.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(tr)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    # Classification by group. The directory is where a person records what kind
    # of thing a principal is, so it is the foundation this section stands on --
    # and when it cannot be read, the section says so instead of reporting an
    # empty finding, which would read identically to a clean estate.
    story.extend(_section_block(7))
    if not data["groups_readable"]:
        story.append(Paragraph(
            "Not attested. The reporting identity could not read the directory, so nothing "
            "below is classified. Directory.Read.All on the reporting identity is required.",
            note))
    else:
        groups = data["group_descriptions"]
        if groups:
            rows = [["Group", "Managed by", "What it means"]]
            for name in sorted(groups):
                rows.append([Paragraph(f"<b>{name}</b>", cell),
                             Paragraph(", ".join(data["group_owners"].get(name, []))
                                       or "nobody", cell),
                             Paragraph(groups[name] or "", cell)])
            tg = Table(rows, colWidths=[1.7 * inch, 2.0 * inch, 5.7 * inch],
                       hAlign="LEFT", repeatRows=1)
            tg.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BAND),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            story.append(tg)
            story.append(Spacer(1, 8))
        if data["ungrouped"]:
            story.append(Paragraph(
                f"{len(data['ungrouped'])} principal(s) hold rights and belong to no group: "
                f"{', '.join(data['ungrouped'])}.", note))
        else:
            story.append(Paragraph(
                "Every principal holding rights belongs to a group.", body))

    story.extend(_section_block(8))
    if data["vault_wide"]:
        vw = [["Principal", "Right", "Where", "Access to every secret"]]
        for v in data["vault_wide"]:
            vw.append([Paragraph(f"<b>{v['principal']}</b>", cell),
                       Paragraph(v["role"], cell),
                       Paragraph(v["where"], cell),
                       Paragraph(v["access"], cell)])
        vt = Table(vw, colWidths=[2.3 * inch, 2.4 * inch, 3.0 * inch, 1.65 * inch],
                   hAlign="LEFT", repeatRows=1)
        vt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(vt)
    else:
        story.append(Paragraph("No principal holds access to every secret in a vault.",
                               body))

    # Classification by group. The directory is where a person records what kind
    # of thing a principal is, so it is the foundation this section stands on --

    story.append(Paragraph(
        f"Orphaned assignments &mdash; grants whose principal no longer exists "
        f"({len(orphaned)})", sub_sec))
    if orphaned:
        o_rows = [["Principal object id", "Right", "Resource", "Subscription"]]
        for h in orphaned:
            for g in h["grants"]:
                o_rows.append([
                    Paragraph(h["object_id"], cell),
                    Paragraph(g["role"], cell),
                    Paragraph(_reaches(g["raw_scope"], g["subscription"],
                                       data["subscription_contents"]), cell),
                    Paragraph(g["subscription"], cell)])
        ot = Table(o_rows, colWidths=[2.6 * inch, 1.9 * inch, 3.0 * inch, 1.9 * inch],
                   hAlign="LEFT", repeatRows=1)
        ot.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ot)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))

    story.append(Paragraph(
        f"Resources undescribed &mdash; carrying no description tag "
        f"({len(data['undescribed'])})", sub_sec))
    if data["undescribed"]:
        u_rows = [["Resource", "Resource group", "Subscription", "Type"]]
        for r in sorted(data["undescribed"],
                        key=lambda x: (x["subscription"].lower(), x["name"].lower())):
            u_rows.append([Paragraph(r["name"], cell),
                           Paragraph(r["group"] or "&mdash;", cell),
                           Paragraph(r["subscription"], cell),
                           Paragraph(r["type"], cell)])
        ut = Table(u_rows, colWidths=[3.0 * inch, 2.2 * inch, 2.2 * inch, 2.0 * inch],
                   hAlign="LEFT", repeatRows=1)
        ut.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(ut)
    else:
        story.append(Paragraph("<i>no exceptions</i>", note))


    # Classification by group. The directory is where a person records what kind
    # of thing a principal is, so it is the foundation this section stands on --
    story.append(Paragraph("<i>no exceptions</i>", note))

    doc.build(story, onFirstPage=_page_furniture, onLaterPages=_page_furniture,
              canvasmaker=_NumberedCanvas)
    return out_path


def send(pdf_path: Path, data: dict) -> dict:
    """Mail the report, with the PDF attached.

    The transmission is posted directly rather than through the pipeline's
    notification client: that module lives in the repository and is not
    present where this runs. One POST, the same API, no shared dependency
    between an audit control and operational alerting.
    """
    import base64

    to = _ch_os.environ.get("ENTITLEMENT_REPORT_TO_EMAIL", "").strip()
    api_key = _ch_os.environ.get("SPARKMAIL_API_KEY", "").strip()
    sender = _ch_os.environ.get("NOTIFICATION_FROM_EMAIL", "noreply@chathealthy.ai").strip()
    missing = [n for n, v in (("ENTITLEMENT_REPORT_TO_EMAIL", to),
                              ("SPARKMAIL_API_KEY", api_key)) if not v]
    if missing:
        raise ChatHealthyException(
            mode="notification_recipient_missing",
            component="EntitlementReport",
            message=f"the report cannot be sent: {', '.join(missing)} absent",
            context={"missing": missing})

    unapproved = [h for h in data["holders"]
                  if not h["approved"] and not h.get("orphaned")]
    orphaned = [h for h in data["holders"] if h.get("orphaned")]
    undeclared = [r for r in data["rightless"] if not r["approved"]]
    stamp = data["generated"].strftime("%Y-%m-%d")
    verdict = ("no exceptions" if not unapproved
               else f"{len(unapproved)} principal(s) outside the approved register")
    body = (
        f"Access entitlement report for {stamp}.\n\n"
        f"Role assignments in force: {data['assignment_count']}\n"
        f"Identities holding rights: {len(data['holders'])}\n"
        f"Outside the approved register: {len(unapproved)}\n\n"
        "The attached PDF states the control, the population and the exceptions, "
        "and lists every right held by every identity.\n"
    )
    payload = {
        "content": {
            "from": sender,
            "subject": f"ChatHealthy access entitlement report {stamp} -- {verdict}",
            "text": body,
            "attachments": [{
                "type": "application/pdf",
                "name": f"ChatHealthy-entitlements-{stamp}.pdf",
                "data": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
            }],
        },
        "recipients": [{"address": to}],
    }
    r = requests.post(
        "https://api.sparkpost.com/api/v1/transmissions",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        data=json.dumps(payload), timeout=120)
    if r.status_code not in (200, 201):
        raise ChatHealthyException(
            mode="notification_send_failed",
            component="EntitlementReport",
            message=f"SparkPost returned {r.status_code}: {r.text[:300]}",
            context={"status": r.status_code, "to": to})
    return {"channel": "email", "status": "sent", "to": to}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily access entitlement report.")
    ap.add_argument("--no-email", action="store_true",
                    help="render the PDF and skip the send")
    ap.add_argument("--out", default="", help="where to write the PDF")
    ap.add_argument("--register-from", default="",
                    help="environment whose deployment_architecture.json supplies the "
                         "approved register (dev|qa|prod), or a full URL. Omitted, the "
                         "register is the one baked into this runbook, or the repository "
                         "copy when running from a working tree.")
    args = ap.parse_args(argv)

    data = collect()
    stamp = data["generated"].strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else Path(
        _ch_os.environ.get("TEMP", ".")) / f"ChatHealthy-entitlements-{stamp}.pdf"
    render_pdf(data, out)

    unapproved = [h for h in data["holders"]
                  if not h["approved"] and not h.get("orphaned")]
    orphaned = [h for h in data["holders"] if h.get("orphaned")]
    undeclared = [r for r in data["rightless"] if not r["approved"]]
    # The attestation qualifier belongs in the summary line, not only in the PDF.
    # "0 exceptions" read from a run that could not see the directory says the
    # same words as one that could, and means something entirely different.
    attested = "attested" if data["groups_readable"] else "NOT ATTESTED (directory unreadable)"
    _LOG.info("entitlement report: %d assignments, %d principals, %d exceptions, "
              "%d ungrouped, %s, pdf=%s",
              data["assignment_count"], len(data["holders"]),
              len(unapproved) + len(data["rightless"]),
              len(data["ungrouped"]), attested, out)

    if args.no_email:
        return 0
    _LOG.info("entitlement report sent: %s", send(out, data).get("status"))
    return 0


def run_as_runbook() -> int:
    """Entry point when Azure Automation runs this on a schedule or webhook.

    Takes no arguments, renders the report and mails it. A failure here is
    silence on an audit control, so it is raised rather than swallowed.
    """
    return main([])


if __name__ == "__main__":
    sys.exit(main())
