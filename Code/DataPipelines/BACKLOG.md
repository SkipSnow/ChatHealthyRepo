# DataPipelines Backlog

## Bugs

### BUG-001: Idle monitor can pause cluster while user is in Atlas GUI

**Status:** Open
**Severity:** Low (dev inconvenience)

The idle monitor fires every 30 minutes and pauses `ChatHealthyDataPipelines` if no active load workers are detected and the last completed pipeline report is older than `IDLE_MONITOR_THRESHOLD_HOURS` (default 2h). It has no awareness of a human browsing the Atlas console — if the threshold is exceeded, it will pause the cluster mid-session.

The Atlas GUI handles it gracefully (shows a paused banner), but any in-flight query stops.

**Workaround:** Manually resume via `ResumeCluster` API call or ScaleUp in the Atlas UI.
**Fix options:** Raise the threshold; add a "session active" flag; or accept it as a dev-cluster tradeoff.
