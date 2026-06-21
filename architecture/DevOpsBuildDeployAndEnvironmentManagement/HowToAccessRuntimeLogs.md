# How to access runtime logs (FindCare / EvaluateCare / SharedServices)

Owner: DevOps. Operator-facing reference so neither operator nor Claude
has to re-discover this on each 503.

## TL;DR per environment

| env   | runtime substrate | how Claude reads the log | how the operator reads the log |
|-------|-------------------|--------------------------|--------------------------------|
| local | docker containers (`ch-findcare`, `ch-evalcare`, `ch-sharedsvc`, `ch-website`) | `docker exec <container> cat //app/logs/chathealthy.log` (note the double slash on Git-Bash to bypass MSYS path translation) | same |
| dev   | HF Spaces (`SkipSnow/dev_<svc>Space_<build_n>`) | **see Section 3 — not currently reachable via API** | HF web UI → Logs tab |
| qa    | HF Spaces (`SkipSnow/qa_<svc>Space_<build_n>`) | same as dev | same as dev |
| prod  | HF Spaces (`SkipSnow/<svc>Space_<build_n>`)    | same as dev | same as dev |

## 1. Where the log is written (inside the container)

`FrontEndApplicationLib/src/chathealthy_frontend_lib/logging_service.py`:

```python
dest = os.environ.get("CH_LOG_DESTINATION", "./logs")
```

So the log goes to `${CH_LOG_DESTINATION}/chathealthy.log` if the env var is
set, otherwise `./logs/chathealthy.log` relative to the container's working
directory.

For our HF Spaces today: `WorkingDir=/app`, `CH_LOG_DESTINATION` is **not
set** on any environment → log lives at `/app/logs/chathealthy.log` inside
the container. `CH_LOG_LEVEL=DEBUG` is set on dev (confirmed via HF
variables API).

## 2. Local: docker exec

```bash
# anywhere under c:/CHATHealthyLLC, in Git Bash
docker exec ch-sharedsvc sh -c 'tail -200 //app/logs/chathealthy.log'
docker exec ch-sharedsvc sh -c "grep -E '503|ERROR|Traceback' //app/logs/chathealthy.log | tail -50"
```

The `//app/...` double-slash is a Git-Bash / MSYS quirk: a single `/app`
gets rewritten to a Windows path that the container does not have. Use
`//app` to send the literal `/app` into the container.

## 3. Dev / qa / prod: HF Spaces

**Current state (2026-06-21): runtime logs are NOT externally reachable.**
HF's public API does not expose container logs:

- `GET /api/spaces/{org}/{space}/logs/container` → 404
- `GET /api/spaces/{org}/{space}/logs/run` → 404 / 401
- `GET /api/spaces/{org}/{space}/logs/build`  → 401 (build logs only, not runtime)

The dev SS Space has no persistent storage attached (confirmed via the
Space metadata API — no `storage`/`persistentStorage` field). That means
`/app/logs/chathealthy.log` is **ephemeral**: it's wiped on every container
restart (and we restart per-build under the per-build-Space-name flow).

So `2026-06-21 03:00Z 503` symptoms are *not currently traceable from
outside*. They exist in container memory until the container exits, then
they're gone.

### Operator path that works today: HF web UI

For dev SS: <https://huggingface.co/spaces/SkipSnow/dev_SharedServicesSpace_<build_n>>
→ **Logs** tab in the HF UI (top of the Space page). Logs there are
truncated and re-paginated; copy-paste relevant lines into the chat for
Claude to inspect.

### Two ways to make Claude-readable runtime logs (recommended next step)

Either is a separate piece of work; both require deploy-chain changes.

1. **`/debug/log` endpoint per backend.** Each backend exposes a GET that
   returns the tail of `chathealthy.log`. Requires a small Python route
   per backend + a secret token guard. Survives only as long as the
   container does (still ephemeral underneath).
2. **HF persistent storage + log destination set to it.** Attach
   persistent storage to each Space (HF UI → Settings → Storage), set
   `CH_LOG_DESTINATION` to the storage mount (`/data` is the HF
   convention), redeploy. Logs survive restarts. Reading still requires
   option 1 OR a one-time download via HF UI.

Both options are blocked on a deploy-chain enhancement; neither is in
scope until the operator says so.

## 4. Operator log paths (not runtime)

Per `_oneshots/deployment_architecture_combined_audit_Final2026-06-20_V4.docx`
§2.4:

| Path / mechanism                                     | What it captures                           |
|------------------------------------------------------|---------------------------------------------|
| `%TEMP%/chathealthy_consumer_errors.log`             | Kafka consumer errors (workstation)        |
| `_oneshots/test_output/instructions_loaded.log`      | Boot's instruction-load audit              |
| `_oneshots/test_output/deploy/deploy_local_{ts}.json`| LocalDeploy structured output              |
| `architecture/EngineeringRuleEnforcement/.../commit_authorization.log` | Pre-commit auth gate (gitignored) |

These are on the operator's workstation, not in any container.

## 5. Quick recipes

### Find 503s in local SS log

```bash
docker exec ch-sharedsvc sh -c "grep -nE '503|chFatalError|gate.*HTTP' //app/logs/chathealthy.log | tail -40"
```

### Show last N seconds of SS log (live tail)

```bash
docker exec -it ch-sharedsvc sh -c "tail -f //app/logs/chathealthy.log"
```

### When the dev backend reports 503 but the API ping returns 200

Means the wrapper's `chFatalError` overlay fired — a browser-side fetch
to /gate or /health failed. Sources to check, in order:

1. Browser console (F12) — the error message includes which URL failed.
2. HF web UI Logs tab on the implicated Space — look for the same
   timestamp.
3. If neither shows the error, the 503 is browser-side (TLS handshake
   refused, ad-blocker, etc.) and the server never saw the request.
