"""Rule-001 enforcement worker — publish Claude Code hook payloads to
the conversation_log Kafka topic via the local producer sidecar.

Wired to Claude Code's UserPromptSubmit and Stop hooks via
.claude/settings.json. Reads the hook payload from stdin, POSTs it to
http://127.0.0.1:8100/produce. If the sidecar is unreachable, launches
a browser window pointed at a viewport-filling failure page that tells
the operator the conversation-log pipeline is down.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


SIDECAR_URL = "http://127.0.0.1:8100/produce"
SIDECAR_POST_TIMEOUT_SECONDS = 2

FAILURE_PAGE_PATH = Path(tempfile.gettempdir()) / "conversation_log_pipeline_down.html"
FAILURE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversation log pipeline is DOWN</title>
<style>
  html, body { margin: 0; padding: 0; height: 100vh; width: 100vw;
               overflow: hidden; background: #b00020; color: #fff;
               font-family: -apple-system, "Segoe UI", Arial, sans-serif; }
  .panel { position: fixed; inset: 0; display: flex;
           flex-direction: column; justify-content: center; align-items: center;
           text-align: center; padding: 2rem; box-sizing: border-box; }
  h1 { font-size: 4.5rem; margin: 0 0 1.5rem; }
  p  { font-size: 1.5rem; margin: 0.5rem 0; max-width: 64rem; line-height: 1.4; }
  code { background: rgba(0,0,0,0.25); padding: 0.1em 0.4em; border-radius: 0.25em; }
</style>
</head>
<body>
<div class="panel">
  <h1>Conversation log pipeline is DOWN</h1>
  <p>The local Kafka producer sidecar at <code>http://127.0.0.1:8100/produce</code> is unreachable.</p>
  <p>Claude Code interactions are NOT being persisted to the conversation log.</p>
  <p>Start the producer sidecar, then reload this page once it is back up.</p>
</div>
</body>
</html>"""


def _post_to_sidecar(payload: dict, hook_event: str) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SIDECAR_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Hook-Event": hook_event,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=SIDECAR_POST_TIMEOUT_SECONDS)
        return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _show_failure_page() -> None:
    FAILURE_PAGE_PATH.write_text(FAILURE_PAGE_HTML, encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW | 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(FAILURE_PAGE_PATH)],
        creationflags=flags,
        close_fds=True,
    )


def main() -> int:
    payload = json.load(sys.stdin)
    hook_event = payload.get("hook_event_name", "")

    if _post_to_sidecar(payload, hook_event):
        return 0

    _show_failure_page()
    return 1


if __name__ == "__main__":
    sys.exit(main())
