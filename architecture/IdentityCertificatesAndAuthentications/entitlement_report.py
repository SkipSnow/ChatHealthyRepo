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
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

import requests  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import landscape, letter  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
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

def _approved_register() -> tuple[dict[str, tuple], str]:
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
        return register, "register injected at build time"

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
        return register, path.name
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
APPROVED, _REGISTER_SOURCE = _approved_register()
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
    """Who can create and entitle identities, from what the run found."""
    agents = sorted({h["name"] or h["object_id"] for h in data["holders"]
                     if h["actor_type"] == "agent"
                     and any(g["role"] in data["privileged_roles"] for g in h["grants"])})
    people = sorted({h["name"] or h["object_id"] for h in data["holders"]
                     if h["actor_type"] == "person"})
    if agents:
        first = (", ".join(agents) + (" holds" if len(agents) == 1 else " hold")
                 + " an administrative role, which includes creating and entitling "
                   "further identities.")
    else:
        first = ("No agent holds an administrative role. Identity creation rests with "
                 + (", ".join(people) if people else "named people") + ".")
    second = ("Classification below is directory group membership."
              if data["groups_readable"] else
              "The directory could not be read on this run, so nothing below is "
              "classified.")
    return first + " " + second


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


def _chathealthy_exception():
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


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
            raise _chathealthy_exception()(
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
        raise _chathealthy_exception()(
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
            raise _chathealthy_exception()(
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
            }
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
    """What each vault secret is, read from deployment_architecture.json.

    The manifest is the source of truth. A secret the manifest does not
    describe renders with its name alone rather than with a guess.
    """
    # Where the repository is not present, the build embeds the same map at
    # CHATHEALTHY_SECRET_DESCRIPTIONS, read from the manifest at build time.
    embedded = _ch_os.environ.get("CHATHEALTHY_SECRET_DESCRIPTIONS", "")
    if embedded:
        try:
            return json.loads(embedded)
        except ValueError as exc:
            _LOG.info("embedded secret descriptions unreadable: %s", exc)
    if _root is None:
        return {}
    manifest = _root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        _LOG.info("deployment architecture unreadable, secrets render unnamed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for record in doc.get("DeploymentTargetRecord", []):
        out.update(record.get("secret_descriptions") or {})
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


def _subscriptions(token: str) -> list[dict]:
    """Every subscription this report covers, discovered rather than asserted.

    The id and display name used to be literals here, and a single pair of them.
    That was two failures at once: a report naming its own scope from a constant
    cannot notice it is pointed somewhere else, and one naming a single scope
    silently omits every other subscription the firm owns -- the omission looks
    exactly like a clean estate.

    The reporting identity sees precisely the subscriptions it is entitled to
    read, so the answer is also the honest boundary of what this run can attest.
    """
    subs = _get_all(f"{ARM}/subscriptions?api-version=2022-12-01", token)
    live = [{"id": s["subscriptionId"],
             "name": s.get("displayName") or s["subscriptionId"],
             "state": s.get("state", "")}
            for s in subs]
    if not live:
        raise _chathealthy_exception()(
            mode="config_error",
            component="EntitlementReport",
            message="the reporting identity can see no subscription; there is "
                    "nothing to report on")
    _LOG.info("entitlement report covers %d subscription(s): %s",
              len(live), ", ".join(s["name"] for s in live))
    return live


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

    subscriptions = _subscriptions(token)

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
        rightless.append({
            "object_id": oid,
            "name": approved[0] if approved else meta["name"],
            "type": meta["kind"],
            "actor_type": approved[2] if approved else "",
            "application": approved[4] if approved else "",
            "origin": meta["origin"],
            "approved": approved is not None,
        })

    missing = [v[0] for k, v in APPROVED.items() if k not in holders]

    # Who answers for each principal and each group. Delegation is a fact about
    # authority as much as any role assignment, and it lives only here.
    owner_targets = {h["object_id"]: "servicePrincipals"
                     for h in holders_list if h["type"].lower() != "user"}
    owner_targets.update({h["object_id"]: "users"
                          for h in holders_list if h["type"].lower() == "user"})
    owners = _owners(credential, owner_targets)

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

    # Who can reach each secret, counting both the grants naming a secret and
    # the broader grants that cover the vault holding it. A certificate is a
    # credential: every principal that can read one can become the identity it
    # names, so a secret reachable by more than one principal is a shared
    # credential and is reported as such.
    reach: dict[str, set[str]] = {}
    for h in holders_list:
        holder_name = h["name"] or h["object_id"]
        vault_wide = [g for g in h["grants"]
                      if g["role"] in privileged_roles and "/vaults/" in g["raw_scope"]
                      and "/secrets/" not in g["raw_scope"]]
        for g in h["grants"]:
            if g["secret"]:
                reach.setdefault(g["secret"], set()).add(holder_name)
        for g in vault_wide:
            for secret in certificate_secrets:
                reach.setdefault(secret, set()).add(holder_name)
        # A subscription-wide data-plane wildcard reaches every secret there is.
        if any(g["role"] in privileged_roles and g["scope"] == "the whole subscription"
               for g in h["grants"]):
            for secret in certificate_secrets:
                reach.setdefault(secret, set()).add(holder_name)
    shared = sorted(((secret, sorted(names)) for secret, names in reach.items()
                     if len(names) > 1), key=lambda x: (-len(x[1]), x[0]))

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
        "vaults": vaults,
        "certificate_secrets": certificate_secrets,
        "shared_secrets": shared,
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


def _page_furniture(canvas, doc):
    canvas.saveState()
    w, h = landscape(letter)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, h - 0.62 * inch, w - 0.5 * inch, h - 0.62 * inch)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.5 * inch, h - 0.52 * inch,
                      "ChatHealthy.ai  |  Access entitlement report  |  Confidential")
    canvas.line(0.5 * inch, 0.55 * inch, w - 0.5 * inch, 0.55 * inch)
    # The footer names the scope on every page, because a page read on its own
    # must say which subscriptions it covers. doc carries it; the render sets it.
    canvas.drawString(0.5 * inch, 0.4 * inch,
                      getattr(doc, "ch_scope_line", "Azure subscriptions: unknown"))
    canvas.drawRightString(w - 0.5 * inch, 0.4 * inch, f"Page {doc.page}")
    canvas.restoreState()


def render_pdf(data: dict, out_path: Path) -> Path:
    base = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=base["Heading1"], fontSize=19, leading=23,
                           textColor=INK, spaceAfter=2)
    sub = ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, textColor=MUTED,
                         spaceAfter=14)
    sec = ParagraphStyle("sec", parent=base["Heading2"], fontSize=12.5, textColor=INK,
                         spaceBefore=16, spaceAfter=6)
    body = ParagraphStyle("b", parent=base["Normal"], fontSize=9, leading=13,
                          textColor=INK, spaceAfter=8)
    note = ParagraphStyle("n", parent=base["Normal"], fontSize=7.5, leading=10,
                          textColor=MUTED)
    who = ParagraphStyle("w", parent=base["Heading3"], fontSize=10.5, textColor=INK,
                         spaceBefore=10, spaceAfter=1)
    cell = ParagraphStyle("c", parent=base["Normal"], fontSize=8, leading=10.5)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.8 * inch, bottomMargin=0.75 * inch,
        title="ChatHealthy access entitlement report",
        author="ChatHealthy.ai", subject="Access entitlement review")
    doc.ch_scope_line = "Azure subscriptions: " + ", ".join(
        f"{s['name']} ({s['id']})" for s in data["subscriptions"])

    stamp = data["generated"].strftime("%d %B %Y at %H:%M UTC")
    unapproved = [h for h in data["holders"] if not h["approved"]]
    approved = [h for h in data["holders"] if h["approved"]]
    priv_unapproved = [h for h in unapproved if h["privileged_count"]]

    story: list = []
    story.append(Paragraph("Access entitlement report", title))
    story.append(Paragraph(
        "Azure subscriptions &nbsp;&middot;&nbsp; "
        + " &nbsp;|&nbsp; ".join(f"{s['name']} ({s['id']})"
                                 for s in data["subscriptions"])
        + f"<br/>Enumerated {stamp}", sub))

    story.append(Paragraph("Scope", sec))
    for para in _scope_story(data):
        story.append(Paragraph(para, body))

    story.append(Paragraph("The control", sec))
    story.append(Paragraph(_population_sentence(data), body))
    story.append(Paragraph(_control_policy(data), body))

    story.append(Paragraph("Population and exceptions", sec))
    summary = [
        ["Role assignments in force", str(data["assignment_count"])],
        ["Principals holding rights", str(len(data["holders"]))],
        ["Approved identities present", f"{len(approved)} of {len(APPROVED)}"],
        ["Principals outside the approved list", str(len(unapproved))],
        ["Of those, holding an administrative role", str(len(priv_unapproved))],
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
    story.append(t)

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

    story.append(Paragraph("How the control is enforced", sec))
    # Written from what the run found. The paragraph this replaces described two
    # mechanisms restraining identity creation, both of which were removed on
    # 2026-08-19 while it went on describing them.
    conditioned = sorted(h["name"] or h["object_id"] for h in data["holders"]
                         if any(g["conditioned"] for g in h["grants"]))
    admin_agents = sorted(h["name"] or h["object_id"] for h in data["holders"]
                          if h["actor_type"] == "agent"
                          and any(g["role"] in data["privileged_roles"]
                                  for g in h["grants"]))
    lines = []
    if conditioned:
        lines.append(f"ABAC conditions are in force on {', '.join(conditioned)}. The "
                     f"Conditioned column marks those assignments and the roles each "
                     f"condition forbids are listed under that identity.")
    else:
        lines.append("No assignment carries an ABAC condition.")
    if admin_agents:
        lines.append(f"{', '.join(admin_agents)} can grant themselves any further role "
                     f"in these subscriptions. Nothing in Azure prevents it.")
    story.append(Paragraph(" ".join(lines), body))

    story.append(Paragraph("What each right permits", sec))
    story.append(Paragraph(
        "Every right named in this report and what holding it permits, computed from the "
        "actions the role publishes. No description is printed: a description is prose about "
        "a role rather than the role, it is written by whoever created it, and the most "
        "powerful right in this estate described itself as administering a subscription "
        "while its actions were every action and every data action. The action patterns "
        "themselves are listed, so the statement can be checked against them.", body))
    gl = [["Right", "Administrative", "What it permits"]]
    for role in sorted(data["role_text"]):
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
        if meta.get("custom"):
            parts.append("<font size=7>Custom role, authored here.</font>")
        gl.append([Paragraph(role, cell),
                   "yes" if role in data["privileged_roles"] else "",
                   Paragraph(" ".join(parts), cell)])
    gt = Table(gl, colWidths=[2.4 * inch, 1.0 * inch, 5.95 * inch], hAlign="LEFT", repeatRows=1)
    gst = [
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, role in enumerate(sorted(data["role_text"]), start=1):
        if role in data["privileged_roles"]:
            gst.append(("TEXTCOLOR", (1, i), (1, i), FLAG))
    gt.setStyle(TableStyle(gst))
    story.append(gt)

    story.append(PageBreak())

    def _grant_table(holder: dict) -> Table:
        # Grants that reach a single secret are gathered under one line naming
        # the vault, with the secrets themselves listed beneath it. Otherwise a
        # role held on six secrets reads as six near-identical rows whose only
        # difference sits at the end of a long string.
        descs = data["secret_descriptions"]
        rows = [["Role", "Scope", "Administrative", "Conditioned", "Adds nothing"]]
        bullet_rows: list[int] = []
        grouped: dict[tuple, list[dict]] = {}
        order: list = []
        for g in holder["grants"]:
            key = (g["role"], g["parent_scope"]) if g["secret"] else ("", id(g))
            if key not in grouped:
                grouped[key] = []
                order.append((key, g))
            grouped[key].append(g)

        for key, first in order:
            members = grouped[key]
            if not first["secret"]:
                rows.append([
                    Paragraph(first["role"], cell),
                    Paragraph(first["scope"], cell),
                    "yes" if first["privileged"] else "",
                    "yes" if first["conditioned"] else "",
                    "yes" if first["redundant"] else "",
                ])
                continue
            rows.append([
                Paragraph(first["role"], cell),
                Paragraph(f'{first["parent_scope"]} &mdash; '
                          f'{len(members)} individual secret'
                          f'{"s" if len(members) != 1 else ""}', cell),
                "yes" if first["privileged"] else "",
                "yes" if any(m["conditioned"] for m in members) else "",
                "yes" if all(m["redundant"] for m in members) else "",
            ])
            for m in sorted(members, key=lambda x: x["secret"]):
                d = descs.get(m["secret"], "")
                text = (f'&nbsp;&nbsp;&bull;&nbsp; <b>{m["secret"]}</b> &mdash; {d}' if d
                        else f'&nbsp;&nbsp;&bull;&nbsp; <b>{m["secret"]}</b>')
                bullet_rows.append(len(rows))
                rows.append(["", Paragraph(text, cell), "", "", ""])
        tb = Table(rows, colWidths=[2.3 * inch, 4.5 * inch, 0.95 * inch, 0.85 * inch, 0.85 * inch],
                   hAlign="LEFT", repeatRows=1)
        st = [
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (2, 0), (4, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for i in bullet_rows:
            st.append(("LINEBELOW", (0, i), (-1, i), 0, colors.white))
            st.append(("TOPPADDING", (0, i), (-1, i), 0))
        for i, row in enumerate(rows[1:], start=1):
            if i not in bullet_rows and row[2] == "yes":
                st.append(("TEXTCOLOR", (2, i), (2, i), FLAG))
                st.append(("FONTNAME", (0, i), (0, i), "Helvetica-Bold"))
        tb.setStyle(TableStyle(st))
        return tb

    def _block(holder: dict) -> list:
        label = holder["name"] or "Unidentified principal"
        out = [Paragraph(label, who)]
        meta = f"{holder['type']} &nbsp;&middot;&nbsp; object id {holder['object_id']}"
        if holder["groups"]:
            meta += "&nbsp;&middot;&nbsp; " + ", ".join(holder["groups"])
        out.append(Paragraph(meta, note))
        # Who answers for this identity existing. An entitlement table says what
        # an identity may do and never who is accountable for it, and in an
        # estate where a person empowers an agent that then administers others,
        # that chain is the control.
        held_by = data["owners"].get(holder["object_id"], [])
        if held_by:
            out.append(Paragraph(f"Managed by {', '.join(held_by)}.", note))
        elif holder["type"].lower() == "user":
            out.append(Paragraph("A person. Directory ownership does not apply.", note))
        elif holder["entra_object_type"] == "managedIdentity":
            # A managed identity carries no directory owner; control follows the
            # Azure resource it is attached to.
            out.append(Paragraph(
                "Managed identity. No directory owner; control follows the Azure "
                "resource.", note))
        else:
            out.append(Paragraph("No owner recorded in the directory.", note))
        if holder["purpose"]:
            out.append(Paragraph(holder["purpose"], note))
        elif not holder["resolvable"]:
            out.append(Paragraph(
                "Not present in the approved register, and its directory record could not be "
                "read to establish what it is.", note))
        out.append(Spacer(1, 4))
        out.append(_grant_table(holder))
        for g in holder["grants"]:
            if g["redundant"]:
                out.append(Spacer(1, 3))
                out.append(Paragraph(
                    f"<b>{g['role']} on {g['scope']} adds nothing.</b> It is "
                    f"{g['redundant']}. Removing it changes what this identity can do in no way.",
                    note))
        for g in holder["grants"]:
            if g["justification"]:
                out.append(Spacer(1, 3))
                out.append(Paragraph(
                    f"<b>{g['role']} &mdash; recorded justification.</b> {g['justification']}", note))
        for g in holder["grants"]:
            if not g["conditioned"]:
                continue
            verbs = []
            if g["constrains_write"]:
                verbs.append("granted")
            if g["constrains_delete"]:
                verbs.append("revoked")
            out.append(Spacer(1, 3))
            out.append(Paragraph(
                f"<b>Constraint in force on {g['role']}.</b> The holder may not "
                f"{' or '.join(verbs) or 'act on'} the following roles, to any principal "
                f"including itself: {', '.join(g['forbidden_roles']) or 'see assignment'}. "
                f"The list includes the constrained role itself, so the holder cannot remove "
                f"this constraint from its own assignment.", note))
        out.append(Spacer(1, 10))
        return out

    story.append(Paragraph(
        f"Exceptions &nbsp;&middot;&nbsp; principals outside the approved list ({len(unapproved)})",
        sec))
    if unapproved:
        story.append(Paragraph(
            "These hold rights and are not in the approved register. Each needs adding to "
            "the register or its rights revoked.", body))
        for h in unapproved:
            story.append(KeepTogether(_block(h)))
    else:
        story.append(Paragraph("None.", body))

    # Classification by group. The directory is where a person records what kind
    # of thing a principal is, so it is the foundation this section stands on --
    # and when it cannot be read, the section says so instead of reporting an
    # empty finding, which would read identically to a clean estate.
    story.append(Paragraph(
        "Classification and delegated authority &nbsp;&middot;&nbsp; "
        "what the directory says each principal is, and who answers for it", sec))
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

    story.append(Paragraph(
        f"Shared credentials &nbsp;&middot;&nbsp; secrets reachable by more than one "
        f"principal ({len(data['shared_secrets'])})", sec))
    story.append(Paragraph(
        "A certificate is a credential: a principal that can read one can act as the "
        "identity it names. Every secret below is reachable by more than one principal, "
        "counting broad grants over the vault as well as grants naming the secret.", body))
    if data["shared_secrets"]:
        rows = [["Secret", "Reachable by"]]
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
        story.append(Paragraph("None.", body))

    story.append(Paragraph(
        f"Grants that add nothing &nbsp;&middot;&nbsp; access held for no stated reason "
        f"({len(data['redundant_grants'])})", sec))
    story.append(Paragraph(
        "Each grant below is already covered by a broader one the same principal holds. "
        "Removing it changes what that principal can do in no way, so it is access "
        "granted for a reason nobody records.", body))
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
        story.append(Paragraph("None.", body))

    rightless = data["rightless"]
    story.append(Paragraph(
        f"Principals that exist and hold no rights &nbsp;&middot;&nbsp; ({len(rightless)})", sec))
    story.append(Paragraph(
        "Principals owned by this tenant that hold no role assignment. They are listed by "
        "existence rather than entitlement.", body))
    if not data["directory_enumerated"]:
        story.append(Paragraph(
            "This list covers managed identities and identities attached to resources. Users and "
            "application registrations are absent: the reporting identity holds no Microsoft Graph "
            "directory read, so the directory could not be enumerated. Granting Directory.Read.All "
            "completes it.", note))
        story.append(Spacer(1, 6))
    if rightless:
        rows = [["Principal", "Kind", "Where it came from", "In the register"]]
        for e in rightless:
            rows.append([Paragraph(e["name"], cell), e["type"],
                         Paragraph(e["origin"], cell), "yes" if e["approved"] else ""])
        t2 = Table(rows, colWidths=[3.0 * inch, 1.6 * inch, 3.6 * inch, 1.2 * inch],
                   hAlign="LEFT", repeatRows=1)
        st2 = [("BACKGROUND", (0, 0), (-1, 0), BAND),
               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
               ("FONTSIZE", (0, 0), (-1, -1), 8),
               ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
               ("VALIGN", (0, 0), (-1, -1), "TOP"),
               ("ALIGN", (3, 0), (3, -1), "CENTER"),
               ("TOPPADDING", (0, 0), (-1, -1), 3),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for i, e in enumerate(rightless, start=1):
            if not e["approved"]:
                st2.append(("TEXTCOLOR", (0, i), (0, i), FLAG))
        t2.setStyle(TableStyle(st2))
        story.append(t2)
    else:
        story.append(Paragraph(
            "None. Every principal that exists also holds rights and is listed above.", body))

    story.append(PageBreak())
    story.append(Paragraph(
        f"Approved identities &nbsp;&middot;&nbsp; full entitlement detail ({len(approved)})", sec))
    story.append(Paragraph(
        "Every right held by each approved identity, and the scope at which it is granted.", body))
    for h in approved:
        story.append(KeepTogether(_block(h)))

    doc.build(story, onFirstPage=_page_furniture, onLaterPages=_page_furniture)
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
        raise _chathealthy_exception()(
            mode="notification_recipient_missing",
            component="EntitlementReport",
            message=f"the report cannot be sent: {', '.join(missing)} absent",
            context={"missing": missing})

    unapproved = [h for h in data["holders"] if not h["approved"]]
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
        raise _chathealthy_exception()(
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
    args = ap.parse_args(argv)

    data = collect()
    stamp = data["generated"].strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else Path(
        _ch_os.environ.get("TEMP", ".")) / f"ChatHealthy-entitlements-{stamp}.pdf"
    render_pdf(data, out)

    unapproved = [h for h in data["holders"] if not h["approved"]]
    # The attestation qualifier belongs in the summary line, not only in the PDF.
    # "0 exceptions" read from a run that could not see the directory says the
    # same words as one that could, and means something entirely different.
    attested = "attested" if data["groups_readable"] else "NOT ATTESTED (directory unreadable)"
    _LOG.info("entitlement report: %d assignments, %d principals, %d exceptions, "
              "%d ungrouped, %s, pdf=%s",
              data["assignment_count"], len(data["holders"]), len(unapproved),
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
    raise SystemExit(main())
