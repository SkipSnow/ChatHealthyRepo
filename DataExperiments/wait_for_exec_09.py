"""Wait for exec_09 to reach a TERMINAL state, then print what that state was.

Terminal means either of two things, and both are reported, because a waiter
that only recognises success is silent through a crash and silence is
indistinguishable from still-running:

  COMPLETED -- the run wrote its closing line
  DIED      -- no exec_09 process remains and the closing line is absent

Exits once. One notification, not a stream.
"""
import subprocess
import sys
import time

sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

RESULT = r"C:\CHATHealthyLLC\DataExperiments\exec_09_inference_result.txt"
PW_FILE = r"C:\Users\skips\postgres_superuser_password.txt"
POLL_S = 60
PS = ("(Get-CimInstance Win32_Process | "
      "Where-Object {$_.CommandLine -match 'exec_09_n4_r5'}).ProcessId -join ','")


def alive():
    try:
        o = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                           capture_output=True, text=True, timeout=90)
        return bool(o.stdout.strip())
    except Exception:
        return True          # unknown is not dead; keep waiting


def closed():
    try:
        with open(RESULT, encoding="utf-8", errors="replace") as fh:
            return "written to " in fh.read()
    except OSError:
        return False


def progress():
    try:
        import psycopg2
        pw = open(PW_FILE, encoding="utf-8").read().strip()
        con = psycopg2.connect(host="localhost", port=5432, user="postgres",
                               password=pw, dbname="medicare_join")
        cur = con.cursor()
        cur.execute("""SELECT count(DISTINCT molecule_id), count(*),
                              count(DISTINCT indication), count(DISTINCT icd10_code)
                       FROM llm_crosswalk""")
        r = cur.fetchone()
        con.close()
        return r
    except Exception as exc:
        return ("?", "?", "?", str(exc)[:60])


def main():
    t0 = time.time()
    while True:
        done, up = closed(), alive()
        if done or not up:
            m, rows, inds, codes = progress()
            state = "COMPLETED" if done else "DIED WITHOUT CLOSING"
            mins = (time.time() - t0) / 60.0
            _log.info("%s", "exec_09 %s after %.0f min of waiting" % (state, mins))
            _log.info("%s", "  molecules=%s/937  rows=%s  indications=%s  "
                            "codes=%s" % (m, rows, inds, codes))
            if not done:
                _log.info("%s", "  the closing line is absent, so this run "
                                "did NOT finish its own reporting. "
                                "Persist-first means the batches it completed "
                                "are durable in llm_crosswalk; the remainder "
                                "must be re-fired.")
            _log.info("%s", "  next: DataExperiments/exec_12_rebuild_chain.py "
                            "-- it gates on 937 and refuses a partial build.")
            sys.stdout.flush()
            return 0
        time.sleep(POLL_S)


raise ChatHealthyException(
    "runtime_error", "run stopped")
