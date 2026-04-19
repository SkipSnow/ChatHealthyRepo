# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# DR-009: Before starting any local server, kill all dead and zombie
# processes on the target ports first.
#
# This is the ONLY way to kill processes. No direct kill/taskkill from bash.
#
# Usage: python kill_zombies.py [port1 port2 ...]
# Default ports: 80 443 5173 8000 8001 8002

import os
import signal
import subprocess
import sys

DEFAULT_PORTS = [80, 443, 5173, 8000, 8001, 8002]


def find_pids_on_port(port):
    """Find PIDs listening on a port."""
    pids = set()
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pids.add(int(parts[-1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids


def kill_pid(pid):
    """Kill a process by PID."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def kill_by_name(name):
    """Kill all processes matching a name. RISK-003: Human-authorized exception."""
    killed = 0
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                parts = line.strip().strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        if pid == os.getpid():
                            continue
                        if kill_pid(pid):
                            print(f"Killed {name} PID {pid}")
                            killed += 1
                    except ValueError:
                        pass
    except Exception as e:
        print(f"Error finding {name}: {e}")
    return killed


def main():
    # --kill-name flag: kill by process name (RISK-003: Human-authorized)
    if len(sys.argv) >= 3 and sys.argv[1] == "--kill-name":
        name = sys.argv[2]
        killed = kill_by_name(name)
        if killed == 0:
            print(f"No processes found matching '{name}'")
        else:
            print(f"Killed {killed} '{name}' process(es)")
        return

    ports = [int(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else DEFAULT_PORTS
    total_killed = 0

    for port in ports:
        pids = find_pids_on_port(port)
        for pid in pids:
            if pid == os.getpid():
                continue
            if kill_pid(pid):
                print(f"Killed PID {pid} on port {port}")
                total_killed += 1
            else:
                print(f"Failed to kill PID {pid} on port {port}")

    if total_killed == 0:
        print(f"No zombie processes found on ports {ports}")
    else:
        print(f"Killed {total_killed} zombie process(es)")


if __name__ == "__main__":
    main()
