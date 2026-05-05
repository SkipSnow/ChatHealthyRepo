"""Standalone HF forum poll for thread 175767 (PKI mTLS to HF Spaces proxy).

Runs from Windows Task Scheduler every 2 hours. No Claude dependency, no
external Python deps (urllib + json from stdlib).

Behavior:
  - Fetch the Discourse JSON for the thread.
  - Compare current post count to %TEMP%/hf_mtls_poll_state.json.
  - On increase, append the new posts (author + timestamp + body) to
    %TEMP%/hf_mtls_poll.log so the boot can surface them next SessionStart.
  - On first run, initialize state without firing an alert.

Exit codes:
  0 — ran cleanly (alert or no alert, both 0)
  1 — fetch failed (logged to log file with ERROR tag)
"""
from __future__ import annotations

import datetime
import html
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

URL = (
    "https://discuss.huggingface.co/t/"
    "how-can-one-do-pkii-mutual-authentication-to-hugging-space-spaces-proxy/175767.json"
)
TEMP = Path(tempfile.gettempdir())
STATE_FILE = TEMP / "hf_mtls_poll_state.json"
LOG_FILE = TEMP / "hf_mtls_poll.log"


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(s).strip()


def _fetch() -> dict:
    req = urllib.request.Request(
        URL,
        headers={"User-Agent": "ChatHealthy-HFMTLSPoll/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _append_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + ("\n" if not line.endswith("\n") else ""))


def main() -> int:
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        data = _fetch()
    except Exception as e:
        _append_log(f"[{ts}] ERROR: fetch failed: {type(e).__name__}: {e}")
        return 1

    posts = (data.get("post_stream") or {}).get("posts") or []
    post_count = len(posts)

    if not STATE_FILE.exists():
        STATE_FILE.write_text(
            json.dumps({"post_count": post_count, "initialized": ts, "last_check": ts}),
            encoding="utf-8",
        )
        _append_log(f"[{ts}] INITIALIZED state: post_count={post_count}")
        return 0

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {"post_count": 0}
    prior = int(state.get("post_count") or 0)

    if post_count > prior:
        _append_log(
            f"[{ts}] NEW REPLIES: post_count {prior} -> {post_count} on thread 175767"
        )
        for p in posts[prior:]:
            user = p.get("username") or "?"
            created = p.get("created_at") or "?"
            body = _strip_html(p.get("cooked") or "")[:1500]
            _append_log(f"  - @{user} at {created}:")
            for line in body.splitlines():
                _append_log(f"      {line}")
        _append_log("")

    state["post_count"] = post_count
    state["last_check"] = ts
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
