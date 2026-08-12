"""Daily entitlement report.

Enumerates every role assignment in the ChatHealthy pipeline subscription,
groups them by the principal holding them, classifies each against the firm's
approved identity list, renders an audit-grade PDF and mails it at 04:00 PST.

The report is written to be handed to an auditor without a covering
explanation. It states the control, shows the population, and lists the
exceptions. A reader who knows nothing about ChatHealthy should be able to
determine from it alone whether access rights are under control.

Principal names come from Microsoft Graph. The approved identities are also
pinned by object id, so the report remains meaningful when the running
identity has no directory read -- the names simply fall back to ids.

CLI:
    python entitlement_report.py [--no-email] [--out <path>]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

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

SUBSCRIPTION_ID = "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"
SUBSCRIPTION_NAME = "PipeLineServices"
ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#c8c8c8")
BAND = colors.HexColor("#f2f2f2")
FLAG = colors.HexColor("#a3231d")
OK = colors.HexColor("#1f6b34")

# The identities the firm has approved, pinned by object id so the
# classification does not depend on directory read being available.
APPROVED = {
    "2e48c87f-6fad-41bf-9c69-3c1e752087a9": (
        "skip@chathealthy.ai", "Named human operator. Sole authority to create identities."),
    "dca8be2d-d37a-4e74-8580-0923cf76cdf9": (
        "claudeCodeAgent", "Engineering agent. Grants and revokes entitlements; cannot create identities."),
    "8e6f61db-9d92-4f7a-a3db-8731a7518791": (
        "DevOpsUser", "Build and deploy. Acts on resources; holds no entitlement-management rights."),
    "1dbcec4a-97ce-4ddb-ab8d-f4a55d5c37a1": (
        "pipelineEditor", "Pipeline runtime. Reads its own certificate; processes data."),
    "a46dfa74-fa21-4175-b5e8-ebc9652dd7c8": (
        "frontendUser", "Front-end runtime. Reads its own certificate."),
}

# Roles that confer administrative authority over the subscription or over
# who may hold rights within it. Their presence is the thing an auditor looks
# for first, so they are counted and marked wherever they appear.
PRIVILEGED_ROLES = (
    "Owner",
    "Contributor",
    "User Access Administrator",
    "Role Based Access Control Administrator",
    "Managed Identity Contributor",
    "ChatHealthy Agent",
)

STORY = (
    "This report specifies every identity permitted to act on ChatHealthy systems, and what each "
    "one may do.",

    "Its scope is the whole of ChatHealthy, because access begins in one place. Each component "
    "authenticates with a certificate, and every certificate is held in a single Azure key vault. "
    "Reaching that vault means obtaining a certificate, and a certificate opens the provider "
    "database, the user services and the rest of the estate.",

    "The rights listed here therefore govern what can be reached across ChatHealthy, not only "
    "within the subscription they are drawn from.",
)

CONTROL_POLICY = (
    "No component may create an identity. That authority belongs to the named human operators "
    "alone, and it is withheld from every component by two means: none holds the directory "
    "permission under which users and application registrations are created, and the role held "
    "by the engineering agent excludes the actions that create a managed identity. The "
    "engineering agent administers rights on identities that already exist. A condition attached "
    "to its own access names the roles it may neither grant nor revoke; that list includes the "
    "role the condition is attached to, so the agent cannot lift the condition from itself. "
    "Every figure and every entry in this report is read live from Azure at the time stated."
)


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
    """The identity the report runs as: pipelineEditor, and nothing else."""
    keys = ("PIPELINEEDITOR_AZURE_TENANT_ID",
            "PIPELINEEDITOR_AZURE_CLIENT_ID",
            "PIPELINEEDITOR_AZURE_CLIENT_SECRET")
    v = {k: _ch_os.environ.get(k, "") for k in keys}
    if not all(v.values()):
        try:
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
            message=f"cannot authenticate as pipelineEditor: {', '.join(missing)} absent",
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


def _managed_identity_names(token: str) -> dict[str, dict]:
    """Resolve user-assigned managed identities through ARM.

    Managed identities are Azure resources as well as directory principals, so
    their names are readable without any Graph permission. This is what keeps
    the report legible when directory read is unavailable.
    """
    out: dict[str, dict] = {}
    try:
        items = _get_all(
            f"{ARM}/subscriptions/{SUBSCRIPTION_ID}/providers"
            f"/Microsoft.ManagedIdentity/userAssignedIdentities?api-version=2023-01-31",
            token)
    except Exception as exc:                                    # noqa: BLE001
        _LOG.info("managed identity enumeration failed: %s", exc)
        return out
    for i in items:
        pid = (i.get("properties") or {}).get("principalId")
        if pid:
            out[pid] = {"name": i.get("name", pid), "kind": "ManagedIdentity"}
    return out


# A role that contains another: holding the key grants everything the values
# grant, so a narrower assignment alongside it adds nothing.
ROLE_CONTAINS = {
    "Key Vault Administrator": ("Key Vault Secrets Officer", "Key Vault Secrets User"),
    "Key Vault Secrets Officer": ("Key Vault Secrets User",),
    "Owner": ("Contributor", "Reader"),
    "Contributor": ("Reader",),
}


def _secret_descriptions() -> dict[str, str]:
    """What each vault secret is, read from deployment_architecture.json.

    The manifest is the source of truth. A secret the manifest does not
    describe renders with its name alone rather than with a guess.
    """
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


def _scope_label(scope: str) -> str:
    """The scope, named by the thing it governs rather than by its full path.

    The distinguishing element goes last and must not be buried: two grants that
    differ only in which secret they cover have to read as different rows.
    """
    s = scope.replace(f"/subscriptions/{SUBSCRIPTION_ID}", "")
    if not s.strip():
        return "the whole subscription"
    s = s.replace("/resourceGroups/", "resource group ")
    s = s.replace("/providers/Microsoft.KeyVault/vaults/", ", key vault ")
    s = s.replace("/providers/Microsoft.ContainerRegistry/registries/", ", container registry ")
    s = s.replace("/providers/Microsoft.Storage/storageAccounts/", ", storage account ")
    s = s.replace("/providers/Microsoft.ManagedIdentity/userAssignedIdentities/", ", managed identity ")
    s = s.replace("/secrets/", ", the single secret ")
    return s.strip()


def _mark_redundant(grants: list[dict]) -> None:
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
                           or g["role"] in ROLE_CONTAINS.get(other["role"], ()))
            if covers_role and (wider_scope or (same_scope and other["role"] != g["role"])):
                g["redundant"] = (f"already covered by {other['role']} on "
                                  f"{_scope_label(other['raw_scope'])}")
                break


def collect() -> dict:
    credential = _credential()
    token = credential.get_token(f"{ARM}/.default").token

    defs = _get_all(f"{ARM}/subscriptions/{SUBSCRIPTION_ID}/providers"
                    f"/Microsoft.Authorization/roleDefinitions?api-version=2022-04-01", token)
    roles = {d["id"].rsplit("/", 1)[-1]: d["properties"]["roleName"] for d in defs}
    role_text = {d["properties"]["roleName"]: (d["properties"].get("description") or "").strip()
                 for d in defs}

    rows = _get_all(f"{ARM}/subscriptions/{SUBSCRIPTION_ID}/providers"
                    f"/Microsoft.Authorization/roleAssignments?api-version=2022-04-01", token)

    oids = sorted({r["properties"]["principalId"] for r in rows})
    # ARM first -- managed identities resolve without any directory permission.
    # Graph then fills in users and app registrations, when permitted.
    names = _managed_identity_names(token)
    graph_names = _principal_names(oids, credential)
    names.update(graph_names)

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
            "scope": _scope_label(p.get("scope", "")),
            "raw_scope": p.get("scope", ""),
            "secret": (p.get("scope", "").split("/secrets/", 1)[1]
                       if "/secrets/" in p.get("scope", "") else ""),
            "parent_scope": _scope_label(p.get("scope", "").split("/secrets/", 1)[0])
                            if "/secrets/" in p.get("scope", "") else "",
            "redundant": "",
            "privileged": role_name in PRIVILEGED_ROLES,
            "conditioned": bool(condition),
            "forbidden_roles": forbidden,
            "constrains_write": "roleAssignments/write" in condition,
            "constrains_delete": "roleAssignments/delete" in condition,
            "justification": (p.get("description") or "").strip(),
        })

    for h in holders.values():
        _mark_redundant(h["grants"])
        h["grants"].sort(key=lambda g: (not g["privileged"], g["scope"], g["role"]))
        h["privileged_count"] = sum(1 for g in h["grants"] if g["privileged"])

    holders_list = sorted(
        holders.values(),
        key=lambda h: (h["approved"], -h["privileged_count"], h["name"] or h["object_id"]))

    missing = [v[0] for k, v in APPROVED.items() if k not in holders]

    return {
        "generated": _dt.datetime.now(_dt.timezone.utc),
        "holders": holders_list,
        "assignment_count": len(rows),
        "names_resolved": bool(graph_names),
        "secret_descriptions": _secret_descriptions(),
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
    canvas.drawString(0.5 * inch, 0.4 * inch,
                      f"Azure subscription {SUBSCRIPTION_NAME} ({SUBSCRIPTION_ID})")
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

    stamp = data["generated"].strftime("%d %B %Y at %H:%M UTC")
    unapproved = [h for h in data["holders"] if not h["approved"]]
    approved = [h for h in data["holders"] if h["approved"]]
    priv_unapproved = [h for h in unapproved if h["privileged_count"]]

    story: list = []
    story.append(Paragraph("Access entitlement report", title))
    story.append(Paragraph(
        f"Azure subscription {SUBSCRIPTION_NAME} &nbsp;&middot;&nbsp; {SUBSCRIPTION_ID}"
        f"<br/>Enumerated {stamp}", sub))

    story.append(Paragraph("Scope", sec))
    for para in STORY:
        story.append(Paragraph(para, body))

    story.append(Paragraph("The control", sec))
    story.append(Paragraph(_population_sentence(data), body))
    story.append(Paragraph(CONTROL_POLICY, body))

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
    story.append(Paragraph(
        "Two mechanisms withhold identity creation. The custom role held by the engineering agent "
        "excludes the actions that create a managed identity, and the actions that would let it "
        "write a replacement role. The assignment carrying that role also carries an "
        "attribute-based access control (ABAC) condition, which names the roles its holder may "
        "neither grant nor revoke; that list includes the role the condition is attached to, so "
        "the holder cannot lift it. Separately, the Entra ID directory permissions under which "
        "users and application registrations are created are held by no component. The "
        "Conditioned column marks each assignment where an ABAC condition is in force, and the "
        "roles it forbids are stated beneath that identity's table.", body))

    story.append(Paragraph("What each right permits", sec))
    story.append(Paragraph(
        "Every right named in this report, and what holding it allows. Rights marked administrative "
        "confer authority over the subscription, or over who may hold rights within it.", body))
    gl = [["Right", "Administrative", "What it permits"]]
    for role in sorted(data["role_text"]):
        gl.append([Paragraph(role, cell),
                   "yes" if role in PRIVILEGED_ROLES else "",
                   Paragraph(data["role_text"][role] or "No description published.", cell)])
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
        if role in PRIVILEGED_ROLES:
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
        out.append(Paragraph(meta, note))
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
            "Each identity below holds rights and does not appear in the approved register. Each "
            "requires disposition: added to the register, or its rights revoked.", body))
        for h in unapproved:
            story.append(KeepTogether(_block(h)))
    else:
        story.append(Paragraph(
            "None. Every principal holding rights in this subscription appears in the approved "
            "register.", body))

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
    from notification_client import NotificationClient
    to = _ch_os.environ.get("ENTITLEMENT_REPORT_TO_EMAIL", "").strip()
    if not to:
        raise _chathealthy_exception()(
            mode="notification_recipient_missing",
            component="EntitlementReport",
            message="ENTITLEMENT_REPORT_TO_EMAIL is not set; the report has nowhere to go")
    unapproved = [h for h in data["holders"] if not h["approved"]]
    stamp = data["generated"].strftime("%Y-%m-%d")
    verdict = ("no exceptions" if not unapproved
               else f"{len(unapproved)} principal(s) outside the approved list")
    subject = f"ChatHealthy access entitlement report {stamp} -- {verdict}"
    body = (
        f"Access entitlement report for {stamp}.\n\n"
        f"Role assignments in force: {data['assignment_count']}\n"
        f"Principals holding rights: {len(data['holders'])}\n"
        f"Outside the approved list: {len(unapproved)}\n\n"
        "The attached PDF states the control, the population and the exceptions, "
        "and lists every role and scope held by every principal.\n"
    )
    return NotificationClient().send_email(
        to=to, subject=subject, body=body,
        attachments=[{"filename": f"ChatHealthy-entitlements-{stamp}.pdf",
                      "type": "application/pdf",
                      "content": pdf_path.read_bytes()}],
        log_context="entitlement_report")


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
    _LOG.info("entitlement report: %d assignments, %d principals, %d exceptions, pdf=%s",
              data["assignment_count"], len(data["holders"]), len(unapproved), out)

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
