"""reservation_reaper.py - Azure Automation runbook (Python 3).

Reaps overdue automated cluster_lifecycle reservations and pauses the
Atlas pipeline cluster when no live reservations remain OR when no
client has accessed the cluster in the activity window (R1). On each
tick, deletes prior completed job records for this runbook to keep
job-history size bounded (R2-refined). On uncaught exception writes
to a failure-state blob and sends a SparkPost email on hourly streak
(R3). On success deletes the failure-state blob.

Replaces the cluster_lifecycle_timer in pipeline/Code/function_app.py.
Runs every 5 minutes on schedule SCH-Reaper-5min in the
ChatHealthyJobManager Automation Account.

Also reaps stuck per-pipeline atomic mutex rows in
admin.cluster_lifecycle (rows with kind=="pipeline_lock", added
2026-07-28 in commit 1384700d). A pipeline_lock is released by the
Controller on run wrap; when the Controller never starts (e.g., VM
cloud-init fails to install docker) or dies mid-run, the lock sits
until its 12h TTL blocks every subsequent fire. On each tick the
reaper checks every pipeline_lock past its startup grace and reaps
the row when Pipelines.Log_{env} has no matching Controller heartbeat
in the heartbeat window -- same signal that would tell an operator
"Controller isn't there." Matching pipeline.runs doc is stamped
status=failed reason=controller_never_started_or_died.

Environment (Automation Variables, exposed via os.environ at runtime):
  MONGO_FRONTEND_connectionString - front-end (always-on) cluster MongoDB URI
  ATLAS_PUBLIC_KEY                - Atlas API public key
  ATLAS_PRIVATE_KEY               - Atlas API private key
  ATLAS_PROJECT_ID                - Atlas group/project ID
  ENV_PREFIX                      - "dev" | "qa" | "prod"
  PIPELINE_CLUSTER                - Atlas cluster name (default ChatHealthyDataPipelines)
  ACTIVITY_WINDOW_MINUTES         - int, default 30 (R1)
  AZ_SUBSCRIPTION_ID              - Azure subscription hosting the Automation Account (R2)
  AZ_RESOURCE_GROUP               - RG hosting ChatHealthyJobManager (R2)
  AZ_AUTOMATION_ACCOUNT           - "ChatHealthyJobManager" (R2)
  AZURE_WEBJOBS_STORAGE           - Function App runtime storage account connection string (R3)
  SPARKMAIL_API_KEY               - SparkPost API key (R3)
  NOTIFICATION_FROM_EMAIL         - sender address (R3)
  NOTIFICATION_TO_EMAIL           - recipient address (R3)
"""
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
import os
import sys
import json

import traceback
from datetime import datetime, timezone, timedelta

import requests
from requests.auth import HTTPDigestAuth
from pymongo import MongoClient

try:
    import automationassets
    for k in ("MONGO_FRONTEND_connectionString", "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY",
              "ATLAS_PROJECT_ID", "ENV_PREFIX", "PIPELINE_CLUSTER",
              "ACTIVITY_WINDOW_MINUTES",
              "AZ_SUBSCRIPTION_ID", "AZ_RESOURCE_GROUP", "AZ_AUTOMATION_ACCOUNT",
              "AZURE_WEBJOBS_STORAGE", "SPARKMAIL_API_KEY",
              "NOTIFICATION_FROM_EMAIL", "NOTIFICATION_TO_EMAIL"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    pass

ACTIVITY_WINDOW  = int(os.environ.get("ACTIVITY_WINDOW_MINUTES", "30"))
ENV_PREFIX       = os.environ.get("ENV_PREFIX", "dev")

# CHLS env setup + SRV-bypass MUST happen before the module-level
# ChatHealthyLoggingService() singleton is instantiated on line ~85. The
# reservation_reaper already receives MONGO_FRONTEND_connectionString via
# automationassets, but it arrives in mongodb+srv:// form; AA Python 3
# sandbox cannot resolve _mongodb._tcp SRV records so we translate to the
# non-SRV direct URI before pymongo touches it.
os.environ.setdefault("CH_SPACE_NAME", "reservation-reaper")
os.environ.setdefault("CH_COMPONENT", "reservation-reaper")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
try:
    from chathealthy_frontend_lib.pipeline_boot import srv_to_direct_uri as _srv_to_direct
    _mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString", "")
    if _mongo_uri.startswith("mongodb+srv://"):
        os.environ["MONGO_FRONTEND_connectionString"] = _srv_to_direct(_mongo_uri)
except Exception as _bootstrap_exc:  # noqa: BLE001
    sys.stderr.write(
        f"reservation_reaper: SRV bypass failed "
        f"({type(_bootstrap_exc).__name__}: {_bootstrap_exc}); "
        "falling back to stderr-only logging.\n"
    )
    os.environ["CH_LOG_DESTINATION"] = "stderr"
CLUSTER_NAME     = os.environ.get("PIPELINE_CLUSTER", "ChatHealthyDataPipelines")
MONGO_URI        = os.environ["MONGO_FRONTEND_connectionString"]
ATLAS_PUB        = os.environ["ATLAS_PUBLIC_KEY"]
ATLAS_PRIV       = os.environ["ATLAS_PRIVATE_KEY"]
ATLAS_PROJECT    = os.environ["ATLAS_PROJECT_ID"]
AZ_SUB           = os.environ.get("AZ_SUBSCRIPTION_ID", "")
AZ_RG            = os.environ.get("AZ_RESOURCE_GROUP", "")
AZ_AA            = os.environ.get("AZ_AUTOMATION_ACCOUNT", "ChatHealthyJobManager")
WEBJOBS_STORAGE  = os.environ.get("AZURE_WEBJOBS_STORAGE", "")
SPARKPOST_KEY    = os.environ.get("SPARKMAIL_API_KEY", "")
EMAIL_FROM       = os.environ.get("NOTIFICATION_FROM_EMAIL", "Skip.Snow@mail.chatHealthy.ai")
EMAIL_TO         = os.environ.get("NOTIFICATION_TO_EMAIL", "Skip@chatHealthy.ai")
DB_NAME          = "admin"
COLLECTION       = "cluster_lifecycle"
ATLAS_BASE       = f"https://cloud.mongodb.com/api/atlas/v2/groups/{ATLAS_PROJECT}"
ATLAS_CLUSTER_URL= f"{ATLAS_BASE}/clusters/{CLUSTER_NAME}"
ATLAS_HEADERS    = {
    "Content-Type": "application/json",
    "Accept": "application/vnd.atlas.2023-02-01+json",
}
REAPER_APPNAME   = "reservation_reaper"
FAILURE_BLOB     = "reservation_reaper_failure.json"
FAILURE_CONTAINER = "$root"

log = ChatHealthyLoggingService()


def _pst_now_iso():
    # PDT = UTC-7 May-November; PST = UTC-8 November-March.
    # Use a fixed tz offset derived from current UTC offset for US/Pacific.
    # Simplest correct approach without pytz: compute via timezone.
    # PDT applies 2nd Sun Mar - 1st Sun Nov. For the runbook's purposes
    # the schema only requires a valid ISO8601 datetime with -07:00 or -08:00.
    # Detect roughly via month; this is adequate per the schema's intent.
    now_utc = datetime.now(timezone.utc)
    m = now_utc.month
    is_pdt = 3 < m < 11 or (m == 3 and now_utc.day >= 8) or (m == 11 and now_utc.day < 8)
    offset = timedelta(hours=-7 if is_pdt else -8)
    return now_utc.astimezone(timezone(offset)).isoformat()


def _blob_client():
    if not WEBJOBS_STORAGE:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(WEBJOBS_STORAGE)
        return svc.get_blob_client(container=FAILURE_CONTAINER, blob=FAILURE_BLOB)
    except Exception as ex:
        log.warning("R3: blob client init failed: %r", ex)
        return None


def _read_failure_state():
    bc = _blob_client()
    if bc is None:
        return None
    try:
        data = bc.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except Exception as ex:
        msg = str(ex).lower()
        if "blobnotfound" in msg or "not found" in msg or "404" in msg:
            return None
        log.warning("R3: read failure-state blob raised: %r", ex)
        return None


def _write_failure_state(state):
    bc = _blob_client()
    if bc is None:
        return
    try:
        bc.upload_blob(json.dumps(state, indent=2).encode("utf-8"), overwrite=True)
    except Exception as ex:
        log.warning("R3: write failure-state blob raised: %r", ex)


def _delete_failure_state():
    bc = _blob_client()
    if bc is None:
        return
    try:
        bc.delete_blob()
        log.info("R3: failure-state blob deleted (success path)")
    except Exception as ex:
        msg = str(ex).lower()
        if "blobnotfound" in msg or "not found" in msg or "404" in msg:
            return
        log.warning("R3: delete failure-state blob raised: %r", ex)


def _send_sparkpost_email(subject, body):
    if not SPARKPOST_KEY:
        log.warning("R3: SPARKMAIL_API_KEY absent; cannot send streak email")
        return False
    try:
        resp = requests.post(
            "https://api.sparkpost.com/api/v1/transmissions",
            headers={"Authorization": SPARKPOST_KEY, "Content-Type": "application/json"},
            json={
                "content": {
                    "from": EMAIL_FROM,
                    "subject": subject,
                    "text": body,
                },
                "recipients": [{"address": EMAIL_TO}],
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return True
        log.warning("R3: SparkPost returned %s %s", resp.status_code, resp.text[:200])
        return False
    except Exception as ex:
        log.warning("R3: SparkPost send raised: %r", ex)
        return False


def _maybe_send_streak_email(state, latest_cause):
    if not state.get("failures"):
        return
    try:
        earliest = state["failures"][0].get("failure_time")
        earliest_dt = datetime.fromisoformat(earliest) if earliest else None
    except Exception:
        earliest_dt = None
    if earliest_dt is None:
        return
    now_utc = datetime.now(timezone.utc)
    streak_secs = (now_utc - earliest_dt).total_seconds()
    if streak_secs < 3900:
        return
    emails = state.get("emails_sent") or []
    if emails:
        try:
            last_sent = datetime.fromisoformat(emails[-1]["datetime_sent_pst"])
            if (now_utc - last_sent.astimezone(timezone.utc)).total_seconds() < 3600:
                return
        except Exception:
            pass
    subject = f"[ChatHealthy] ReservationReaper failure streak: {int(streak_secs // 60)} min"
    body = (
        f"The Azure Automation runbook ReservationReaper has been failing continuously "
        f"for {int(streak_secs // 60)} minutes.\n\n"
        f"Earliest failure_time: {earliest}\n"
        f"Latest failure_cause: {latest_cause}\n"
        f"Total failures recorded: {len(state['failures'])}\n\n"
        f"Atlas pipeline cluster auto-pause may not be functioning. "
        f"Inspect job logs in Automation Account ChatHealthyJobManager / "
        f"runbook ReservationReaper."
    )
    if _send_sparkpost_email(subject, body):
        emails.append({
            "datetime_sent_pst": _pst_now_iso(),
            "from": EMAIL_FROM,
            "to": EMAIL_TO,
            "content": body,
        })
        state["emails_sent"] = emails


def _record_failure(cause):
    state = _read_failure_state() or {"emails_sent": [], "failures": []}
    state.setdefault("emails_sent", [])
    state.setdefault("failures", [])
    state["failures"].append({
        "failure_time": datetime.now(timezone.utc).isoformat(),
        "failure_cause": cause[:2000],
    })
    _maybe_send_streak_email(state, cause)
    _write_failure_state(state)


def _r2_purge_old_job_records():
    if not (AZ_SUB and AZ_RG and AZ_AA):
        log.info("R2: skipping job-history purge (Az env vars not set)")
        return
    try:
        idr = os.environ.get("IDENTITY_ENDPOINT")
        idh = os.environ.get("IDENTITY_HEADER")
        if not idr or not idh:
            log.info("R2: no MI endpoint; skipping purge")
            return
        tok = requests.get(
            idr,
            params={"resource": "https://management.azure.com/", "api-version": "2019-08-01"},
            headers={"X-IDENTITY-HEADER": idh}, timeout=15,
        ).json().get("access_token")
        if not tok:
            log.info("R2: MI token fetch returned no access_token; skipping purge")
            return
        h = {"Authorization": f"Bearer {tok}"}
        base = (f"https://management.azure.com/subscriptions/{AZ_SUB}"
                f"/resourceGroups/{AZ_RG}/providers/Microsoft.Automation"
                f"/automationAccounts/{AZ_AA}/jobs")
        r = requests.get(base,
                         params={"api-version": "2023-11-01",
                                 "$filter": "properties/runbook/name eq 'ReservationReaper'"},
                         headers=h, timeout=30)
        if r.status_code != 200:
            log.warning("R2: list-jobs failed %s %s", r.status_code, r.text[:200])
            return
        my_job_id = os.environ.get("AZUREPS_HOST_ENVIRONMENT", "") or os.environ.get("AUTOMATION_JOB_ID", "")
        purged = 0
        for j in r.json().get("value", []):
            jid = j.get("properties", {}).get("jobId") or j.get("name")
            status = j.get("properties", {}).get("status")
            if not jid or jid == my_job_id:
                continue
            if status not in ("Completed", "Failed", "Stopped", "Suspended"):
                continue
            d = requests.delete(f"{base}/{jid}",
                                params={"api-version": "2023-11-01"},
                                headers=h, timeout=15)
            if d.status_code in (200, 204):
                purged += 1
        log.info("R2: purged %d prior ReservationReaper job records", purged)
    except Exception as ex:
        log.warning("R2: purge raised %r; continuing", ex)


# Startup grace for pipeline_lock rows: how long we wait after
# acquired_at before the "Controller silent" test is legitimate. VM
# boot + apt + docker pull + Controller container start is typically
# 3-5 minutes in a healthy fire; 8 min leaves headroom for slow days.
PIPELINE_LOCK_STARTUP_GRACE_MIN = 8

# Controller heartbeat window. ChatHealthyLoggingService writes to
# Pipelines.Log_{env} on every info+ record; a live Controller produces
# many per minute (pymongo pings, WI claims, heartbeats). Five minutes
# of total silence on a run that is past its startup grace means the
# Controller either never started or died. Reap.
PIPELINE_LOCK_HEARTBEAT_MIN = 5


def _reap_stuck_pipeline_lock(coll, log_coll, runs_coll, row, now_aware):
    """Reap a pipeline_lock row iff its Controller has been silent.

    Two-signal test:
      (a) row's acquired_at is at least PIPELINE_LOCK_STARTUP_GRACE_MIN
          ago (VM boot + docker pull + Controller start should be done).
      (b) Pipelines.Log_{env} has ZERO documents matching
          {job_id: <row.run_id>, timeStamp: >= now - PIPELINE_LOCK_HEARTBEAT_MIN}.

    Both conditions must hold. If they do, the lock's owning Controller
    never started or has died -- release the lock so the next fire can
    proceed. Atomic delete_one filter includes both _id and run_id so
    a concurrent legitimate release cannot race with us.

    Also deletes the matching legacy reservation row (whose _id equals
    row.run_id) and stamps pipeline.runs status=failed with a reason.

    Returns True iff this row was reaped.
    """
    acquired_at = row.get("acquired_at")
    if isinstance(acquired_at, str):
        try:
            acquired_at = datetime.fromisoformat(acquired_at)
        except (ValueError, TypeError):
            return False
    if not acquired_at:
        return False
    if getattr(acquired_at, "tzinfo", None) is None:
        acquired_at = acquired_at.replace(tzinfo=timezone.utc)
    age_min = (now_aware - acquired_at).total_seconds() / 60.0
    if age_min < PIPELINE_LOCK_STARTUP_GRACE_MIN:
        return False  # Still inside startup grace; give the Controller time.
    run_id = row.get("run_id")
    if not run_id:
        return False
    # Heartbeat probe: any log document for this run_id in the last
    # PIPELINE_LOCK_HEARTBEAT_MIN minutes. limit=1 stops the count early
    # for the common alive-Controller case (huge Log collection).
    cutoff = now_aware - timedelta(minutes=PIPELINE_LOCK_HEARTBEAT_MIN)
    recent_ct = log_coll.count_documents(
        {"job_id": run_id, "timeStamp": {"$gte": cutoff}},
        limit=1,
    )
    if recent_ct > 0:
        return False  # Controller has emitted recent logs -> alive.
    # Dead. Reap.
    log.info(
        "Reaping stuck pipeline_lock: _id=%s run_id=%s age_min=%.1f "
        "(no logs in last %d min; Controller never started or died)",
        row.get("_id"), run_id, age_min, PIPELINE_LOCK_HEARTBEAT_MIN,
    )
    # Atomic release: filter on _id AND run_id so a concurrent legitimate
    # release by the actual Controller cannot race with us.
    coll.delete_one({"_id": row["_id"], "run_id": run_id})
    # Also delete the matching legacy reservation row (same run_id, in
    # the same collection under _id=run_id).
    coll.delete_one({"_id": run_id})
    # Stamp pipeline.runs for post-mortem.
    try:
        runs_coll.update_one(
            {"run_id": run_id, "status": "running"},
            {"$set": {
                "status": "failed",
                "failure_reason": "controller_never_started_or_died",
                "reaped_by_reservation_reaper_at": now_aware,
            }},
        )
    except Exception as ex:  # noqa: BLE001
        log.warning("Could not stamp pipeline.runs failure for %s: %r",
                    run_id, ex)
    return True


def _r1_client_active_recently(auth, window_ms_start, window_ms_end):
    r = requests.get(
        f"{ATLAS_BASE}/dbAccessHistory/clusters/{CLUSTER_NAME}",
        params={"start": window_ms_start, "end": window_ms_end,
                "authResult": "true"},
        auth=auth, headers=ATLAS_HEADERS, timeout=30,
    )
    if r.status_code != 200:
        log.warning("R1: dbAccessHistory %s - treating as active (fail-safe NO pause)",
                    r.status_code)
        return True
    logs = r.json().get("accessLogs", []) or []
    for entry in logs:
        line = entry.get("logLine") or ""
        if f'"name":"{REAPER_APPNAME}"' in line:
            continue
        if entry.get("authResult") is True:
            log.info("R1: client active - %s @ %s",
                     entry.get("username"), entry.get("timestamp"))
            return True
    return False


def _main():
    now = datetime.now(timezone.utc)
    window_ms_start = int((now - timedelta(minutes=ACTIVITY_WINDOW)).timestamp() * 1000)
    window_ms_end   = int(now.timestamp() * 1000)

    _r2_purge_old_job_records()

    atlas_auth = HTTPDigestAuth(ATLAS_PUB, ATLAS_PRIV)
    pre = requests.get(ATLAS_CLUSTER_URL, auth=atlas_auth,
                       headers=ATLAS_HEADERS, timeout=15)
    pre_state = pre.json().get("stateName", "UNKNOWN")
    pre_paused = pre.json().get("paused", False)
    if pre_state == "IDLE" and pre_paused:
        msg = ("Reaper tick: reaped=0, live_remaining=0, client_active=False, "
               "cluster_state=PAUSED, paused=True, reason=already_paused")
        log.info(msg)
        ChatHealthyLoggingService().info(msg)
        return

    # MONGO_URI came from MONGO_FRONTEND_connectionString env; the utility
    # will read the same env directly.
    client = ChatHealthyMongoUtilities("MONGO_FRONTEND_connectionString").getConnection()
    coll = client[DB_NAME][COLLECTION]
    log_coll = client["Pipelines"][f"Log_{ENV_PREFIX}"]
    runs_coll = client["chathealthyfrontend"]["pipeline.runs"]
    reservations = list(coll.find({}))
    log.info("Loaded %d cluster_lifecycle rows from %s.%s",
             len(reservations), DB_NAME, COLLECTION)

    kept, reaped = [], []
    now_utc_naive = datetime.utcnow()
    now_utc_aware = datetime.now(timezone.utc)
    for r in reservations:
        # Row shape discriminator. Two shapes coexist here:
        #  - kind == "pipeline_lock" -- atomic per-pipeline mutex added
        #    2026-07-28 in commit 1384700d. Fields: _id="pipeline_lock:{name}",
        #    kind, pipeline_name, run_id, vm_name, acquired_at (ISO str),
        #    expires_at (ISO str). Released by Controller on run wrap OR
        #    by this reaper on Controller-silence heartbeat (new below).
        #  - legacy DB reservation -- the original shape. Fields: _id=run_id,
        #    expiry_at (naive datetime), status, requester. Released by
        #    wall-clock TTL only.
        # New pipeline_lock path FIRST because the legacy path treats
        # missing expiry_at as "keep forever" and would silently protect
        # every pipeline_lock row from reaping.
        if r.get("kind") == "pipeline_lock":
            if _reap_stuck_pipeline_lock(coll, log_coll, runs_coll,
                                         r, now_utc_aware):
                reaped.append(r)
            else:
                kept.append(r)
            continue
        # Legacy reservation shape: reap by wall-clock expiry_at.
        expiry_dt = r.get("expiry_at")
        if expiry_dt is None:
            kept.append(r); continue
        if isinstance(expiry_dt, str):
            try:
                expiry_dt = datetime.fromisoformat(expiry_dt)
            except (ValueError, TypeError):
                kept.append(r); continue
        if getattr(expiry_dt, "tzinfo", None) is not None:
            expiry_dt = expiry_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if now_utc_naive <= expiry_dt:
            kept.append(r); continue
        reaped.append(r)
        log.info("Reaping overdue reservation: _id=%s requester=%s expiry_at=%s",
                 r.get("_id", ""), r.get("requester", ""), expiry_dt.isoformat())

    if reaped:
        # Pipeline_lock rows delete themselves inside _reap_stuck_pipeline_lock
        # (needs the guarded _id+run_id filter to prove atomic ownership).
        # Legacy reservation rows delete here by _id.
        legacy_ids = [r["_id"] for r in reaped if r.get("kind") != "pipeline_lock"]
        if legacy_ids:
            coll.delete_many({"_id": {"$in": legacy_ids}})

    live = [r for r in kept if r.get("status", "active") == "active"]

    # Precedence: the reservation queue is the primary signal (REQ-B-001). While a
    # live reservation exists the cluster stays up, full stop. dbAccessHistory is a
    # lower-precedence confirmatory signal that only runs once the queue is empty;
    # if something is still hitting the cluster despite no reservations, hold off on
    # the pause and surface that as unaccounted use.
    should_pause = False
    pause_reason = ""
    client_active = None
    if not live:
        client_active = _r1_client_active_recently(
            atlas_auth, window_ms_start, window_ms_end)
        if not client_active:
            should_pause = True
            pause_reason = "no_live_reservations"
        else:
            pause_reason = "no_reservations_but_client_active"
            log.warning("Pause withheld: no live reservations but cluster has "
                        "recent client access; possible unaccounted use")

    cluster_state = "UNKNOWN"
    paused = False
    pause_failure_detail = ""
    if should_pause:
        resp = requests.get(ATLAS_CLUSTER_URL, auth=atlas_auth,
                            headers=ATLAS_HEADERS, timeout=15)
        cluster_state = resp.json().get("stateName", "UNKNOWN")
        if cluster_state == "IDLE":
            resp = requests.patch(
                ATLAS_CLUSTER_URL, json={"paused": True},
                auth=atlas_auth, headers=ATLAS_HEADERS, timeout=30,
            )
            if resp.status_code in (200, 202):
                paused = True
                log.info("Cluster %s paused (reason=%s, Atlas response: %s)",
                         CLUSTER_NAME, pause_reason,
                         resp.json().get("stateName", "?"))
            else:
                try:
                    j = resp.json()
                    pause_failure_detail = j.get("errorCode", "") or j.get("detail", "")
                except Exception:
                    pause_failure_detail = f"http_{resp.status_code}"
                log.warning("Cluster %s pause REJECTED: %s %s",
                            CLUSTER_NAME, resp.status_code, pause_failure_detail)
                if pause_failure_detail:
                    pause_reason = f"{pause_reason}|atlas_rejected:{pause_failure_detail}"

    msg = (f"Reaper tick: reaped={len(reaped)}, live_remaining={len(live)}, "
           f"client_active={client_active}, cluster_state={cluster_state}, "
           f"paused={paused}, reason={pause_reason or 'none'}")
    log.info(msg)
    ChatHealthyLoggingService().info(msg)


try:
    _main()
    _delete_failure_state()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Reaper tick failed: %s", tb)
    try:
        _record_failure(tb)
    except Exception as inner:
        log.error("R3: failure-state recording also failed: %r", inner)
    sys.exit(1)
