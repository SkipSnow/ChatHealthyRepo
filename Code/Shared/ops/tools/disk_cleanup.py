#!/usr/bin/env python3
"""
ChatHealthy.ai — Disk Cleanup Script
=====================================
Uninstalls unused programs, removes data directories, cleans PATH and registry.

Usage:
    python disk_cleanup.py --dry-run     # Preview what will be removed (SAFE)
    python disk_cleanup.py --execute     # Actually remove everything (DESTRUCTIVE)

Run as Administrator (required for uninstalls and registry edits).

Copyright (c) 2026 Skip Snow. All rights reserved.
"""

import os
import sys
import shutil
import subprocess
import winreg
import argparse
import logging
from datetime import datetime
from pathlib import Path

# ── Logging ────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), f"disk_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cleanup")

# ── Configuration ──────────────────────────────────────────

# Programs to uninstall (search terms matched against DisplayName in registry)
UNINSTALL_PROGRAMS = [
    "Unity",
    "Unity Hub",
    "Blender",
    "Visual Studio",       # All versions
    "JetBrains",           # PyCharm etc
    "PyCharm",
    "MongoDB",
    "MongoDB Compass",
    "QGIS",
    "WildTangent",
    "CoffeeCup",
    "Xerox",
    "Ollama",
    ".NET SDK",            # VS .NET SDKs (keep Runtime)
    ".NET Targeting Pack",
    ".NET AppHost",
    ".NET Toolset",
    ".NET Templates",
    ".NET MAUI",
    "Microsoft.iOS",
    "Microsoft.MacCatalyst",
    "Microsoft.NETCore.App.Runtime.AOT",
    "Microsoft.NETCore.App.Runtime.Mono",
    "Microsoft.NET.Runtime.Mono",
    "Microsoft.NET.Sdk.Android",
    "Microsoft.NET.Sdk.iOS",
    "Microsoft.NET.Sdk.tvOS",
    "Microsoft.NET.Sdk.macOS",
    "Microsoft.NET.Sdk.MacCatalyst",
    "Microsoft.NET.Sdk.Maui",
    "Microsoft.NET.Sdk.Aspire",
    "Microsoft.NET.Workload",
    "Microsoft ASP.NET Core Module",
]

# Programs to KEEP — never uninstall even if name partially matches above
KEEP_PROGRAMS = [
    "Microsoft .NET Runtime",       # Keep runtime for Azure Functions
    "Microsoft .NET Host",          # Keep host
    "Microsoft .NET Host FX",       # Keep host FX resolver
    "Microsoft ASP.NET Core.*Shared Framework",  # Keep shared frameworks
    "Docker",
    "McAfee",
    "Microsoft Office",
    "Microsoft Edge",               # Keep Edge itself, just clear data
    "Microsoft Teams",
    "Cursor",
    "Visual Studio Code",   # Cursor is built on VS Code — never uninstall
    "Git",
    "Python",
    "Node",
    "npm",
    "Cloudflare",
    "WindowsApps",          # Windows system path
]

# Data directories to remove
DATA_DIRS = [
    # Unity
    r"C:\Users\skips\AppData\Local\Unity",
    r"C:\Users\skips\AppData\LocalLow\Unity",
    r"C:\Users\skips\AppData\Roaming\Unity",
    r"C:\Users\skips\AppData\Local\UnityHub",
    r"C:\Users\skips\Documents\com.unity.multiplayer.samples.coop-2.0.4",
    r"C:\Users\skips\OneDrive\Documents\unity",
    r"C:\Users\skips\OneDrive\Documents\GitHub\com.unity.multiplayer.samples.bitesize",
    r"C:\Users\skips\OneDrive\Documents\Studio Artifacts\UnityStuff",
    # Apple / iTunes
    r"C:\Users\skips\Apple",
    r"C:\Users\skips\Music\iTunes",
    r"C:\Users\skips\AppData\Local\Apple",
    r"C:\Users\skips\AppData\Local\Apple Computer",
    r"C:\Users\skips\AppData\Roaming\Apple Computer",
    # Visual Studio
    r"C:\Users\skips\AppData\Local\Microsoft\VisualStudio",
    r"C:\Users\skips\AppData\Local\NuGet",
    r"C:\Users\skips\.nuget",
    r"C:\Users\skips\OneDrive\Documents\Visual Studio 2013",
    # JetBrains / PyCharm
    r"C:\Users\skips\AppData\Local\JetBrains",
    r"C:\Users\skips\AppData\Roaming\JetBrains",
    r"C:\Users\skips\PycharmProjects",
    # Ollama
    r"C:\Users\skips\.ollama",
    # MongoDB
    r"C:\Users\skips\AppData\Local\MongoDBCompass",
    r"C:\Users\skips\AppData\Local\MongoDB",
    # CoffeeCup
    r"C:\Users\skips\AppData\Local\CoffeeCup Software",
    r"C:\Users\skips\AppData\Roaming\CoffeeCup Software",
    r"C:\Users\skips\OneDrive\Documents\CoffeeCup Software",
    # Blender
    r"C:\Users\skips\AppData\Local\Blender Foundation",
    r"C:\Users\skips\AppData\Roaming\Blender Foundation",
    # QGIS
    r"C:\Users\skips\AppData\Local\QGIS",
    r"C:\Users\skips\AppData\Roaming\QGIS",
    # WildTangent
    r"C:\Users\skips\AppData\Local\WildTangent",
    # Xerox
    r"C:\Users\skips\AppData\Local\Xerox",
    # Microsoft Edge cache (keep browser, clear cache)
    r"C:\Users\skips\AppData\Local\Microsoft\Edge\User Data\Default\Cache",
    r"C:\Users\skips\AppData\Local\Microsoft\Edge\User Data\Default\Code Cache",
]

# Program directories to force-remove if uninstaller misses them
FORCE_REMOVE_DIRS = [
    r"C:\Program Files\Unity",
    r"C:\Program Files\Unity 2021.3.22f1",
    r"C:\Program Files\Unity Hub",
    r"C:\Program Files\Blender Foundation",
    r"C:\Program Files\QGIS 3.34.3",
    r"C:\Program Files\JetBrains",
    r"C:\Program Files\MongoDB",
    r"C:\Program Files\Xerox",
    r"C:\Program Files (x86)\WildTangent Games",
    r"C:\Program Files (x86)\WildGames",
]

# PATH entries to remove (substrings)
PATH_REMOVE = [
    "Unity",
    "Blender",
    "JetBrains",
    "PyCharm",
    "QGIS",
    "MongoDB",
    "WildTangent",
    "CoffeeCup",
    "Ollama",
    "dotnet",  # Will be re-evaluated — keep if .NET Runtime path
]

PATH_KEEP = [
    "dotnet\\shared",  # Keep .NET shared runtime path
    "npm",             # Node package manager — needed for React
    "WindowsApps",     # Windows system path
]


# ── Helpers ────────────────────────────────────────────────

def get_installed_programs():
    """Read all installed programs from registry."""
    programs = []
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in reg_paths:
        try:
            key = winreg.OpenKey(hive, path)
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        uninstall_str = ""
                        try:
                            uninstall_str = winreg.QueryValueEx(subkey, "UninstallString")[0]
                        except FileNotFoundError:
                            pass
                        quiet_str = ""
                        try:
                            quiet_str = winreg.QueryValueEx(subkey, "QuietUninstallString")[0]
                        except FileNotFoundError:
                            pass
                        size = 0
                        try:
                            size = winreg.QueryValueEx(subkey, "EstimatedSize")[0]
                        except (FileNotFoundError, TypeError):
                            pass
                        programs.append({
                            "name": name,
                            "key": subkey_name,
                            "hive": hive,
                            "path": path,
                            "uninstall": uninstall_str,
                            "quiet_uninstall": quiet_str,
                            "size_kb": size,
                        })
                    except FileNotFoundError:
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except OSError:
                    pass
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass
    return programs


def should_uninstall(name):
    """Check if a program should be uninstalled."""
    import re
    # Check keep list first
    for keep in KEEP_PROGRAMS:
        if re.search(keep, name, re.IGNORECASE):
            return False
    # Check uninstall list
    for term in UNINSTALL_PROGRAMS:
        if re.search(re.escape(term), name, re.IGNORECASE):
            return True
    return False


def uninstall_program(prog, dry_run=True):
    """Uninstall a program using its uninstall string."""
    name = prog["name"]
    cmd = prog.get("quiet_uninstall") or prog.get("uninstall", "")
    if not cmd:
        log.warning("  SKIP (no uninstall command): %s", name)
        return False

    # Make it quiet/silent
    if "/I{" in cmd:
        cmd = cmd.replace("/I{", "/X{")  # Change modify to uninstall
    if "msiexec" in cmd.lower() and "/quiet" not in cmd.lower():
        cmd += " /quiet /norestart"
    elif "msiexec" not in cmd.lower() and "--silent" not in cmd.lower():
        # Try common silent flags
        if "/S" not in cmd and "/s" not in cmd:
            cmd += " /S"

    if dry_run:
        log.info("  DRY RUN uninstall: %s", name)
        log.info("    Command: %s", cmd[:120])
        return True

    log.info("  UNINSTALLING: %s", name)
    log.info("    Command: %s", cmd[:120])
    try:
        result = subprocess.run(cmd, shell=True, timeout=300, capture_output=True, text=True)
        if result.returncode == 0:
            log.info("    SUCCESS: %s", name)
            return True
        else:
            log.warning("    FAILED (code %d): %s — %s", result.returncode, name, result.stderr[:200])
            return False
    except subprocess.TimeoutExpired:
        log.warning("    TIMEOUT: %s (killed after 5 min)", name)
        return False
    except Exception as e:
        log.error("    ERROR: %s — %s", name, e)
        return False


def remove_directory(path, dry_run=True):
    """Remove a directory tree."""
    if not os.path.exists(path):
        return 0

    try:
        size = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    except Exception:
        size = 0

    size_gb = round(size / (1024 ** 3), 2)

    if dry_run:
        log.info("  DRY RUN remove: %s (%.2f GB)", path, size_gb)
        return size

    log.info("  REMOVING: %s (%.2f GB)", path, size_gb)
    try:
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            log.warning("    PARTIAL: some files locked in %s", path)
        else:
            log.info("    DONE: %s", path)
        return size
    except Exception as e:
        log.error("    ERROR: %s — %s", path, e)
        return 0


def clean_path(dry_run=True):
    """Remove dead entries from system and user PATH."""
    cleaned = 0

    for scope, hive, key_path in [
        ("User", winreg.HKEY_CURRENT_USER, r"Environment"),
        ("System", winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]:
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            path_value, _ = winreg.QueryValueEx(key, "Path")
            entries = path_value.split(";")
            original_count = len(entries)

            new_entries = []
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue

                # Check if it should be removed
                remove = False
                for term in PATH_REMOVE:
                    if term.lower() in entry.lower():
                        # Check keep exceptions
                        keep = False
                        for k in PATH_KEEP:
                            if k.lower() in entry.lower():
                                keep = True
                                break
                        if not keep:
                            remove = True
                            break

                # Also remove paths that don't exist — but check PATH_KEEP first
                if not remove and not os.path.exists(entry):
                    # Don't remove if it matches a keep pattern (may use env vars like %USERPROFILE%)
                    keep_it = False
                    for k in PATH_KEEP:
                        if k.lower() in entry.lower():
                            keep_it = True
                            break
                    if not keep_it and "%" not in entry:
                        # Only remove non-variable paths that truly don't exist
                        remove = True

                if remove:
                    log.info("  %s PATH remove: %s", scope, entry)
                    cleaned += 1
                else:
                    new_entries.append(entry)

            if cleaned > 0 and not dry_run:
                new_path = ";".join(new_entries)
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
                log.info("  %s PATH updated: %d → %d entries", scope, original_count, len(new_entries))

            winreg.CloseKey(key)
        except Exception as e:
            log.warning("  Could not clean %s PATH: %s", scope, e)

    return cleaned


def clean_registry(dry_run=True):
    """Remove orphaned uninstall registry entries for removed programs."""
    cleaned = 0
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hive, path in reg_paths:
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            to_delete = []

            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        install_loc = ""
                        try:
                            install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                        except FileNotFoundError:
                            pass

                        # If program was in our uninstall list and install location is gone
                        if should_uninstall(name) and install_loc and not os.path.exists(install_loc):
                            to_delete.append((subkey_name, name))
                    except FileNotFoundError:
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except OSError:
                    pass

            for subkey_name, name in to_delete:
                if dry_run:
                    log.info("  DRY RUN registry remove: %s (%s)", name, subkey_name)
                else:
                    try:
                        winreg.DeleteKey(key, subkey_name)
                        log.info("  REMOVED registry: %s", name)
                    except Exception as e:
                        log.warning("  Could not remove registry key %s: %s", subkey_name, e)
                cleaned += 1

            winreg.CloseKey(key)
        except Exception as e:
            log.warning("  Could not access registry path: %s", e)

    return cleaned


# ── Main ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ChatHealthy.ai Disk Cleanup")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only — no changes")
    group.add_argument("--execute", action="store_true", help="Actually remove everything")
    args = parser.parse_args()

    dry_run = args.dry_run
    mode = "DRY RUN" if dry_run else "EXECUTE"

    log.info("=" * 60)
    log.info("ChatHealthy.ai Disk Cleanup — %s", mode)
    log.info("Log file: %s", LOG_FILE)
    log.info("=" * 60)

    if not dry_run:
        log.info("")
        log.info("*** WARNING: This will permanently delete programs and data. ***")
        log.info("*** Press Ctrl+C within 10 seconds to abort. ***")
        log.info("")
        import time
        try:
            for i in range(10, 0, -1):
                print(f"  Starting in {i}...", end="\r")
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Aborted by user.")
            return

    # Phase 1: Uninstall programs
    log.info("")
    log.info("─── Phase 1: Uninstall Programs ───")
    programs = get_installed_programs()
    uninstall_count = 0
    for prog in programs:
        if should_uninstall(prog["name"]):
            if uninstall_program(prog, dry_run):
                uninstall_count += 1

    log.info("Programs to uninstall: %d", uninstall_count)

    # Phase 2: Remove data directories
    log.info("")
    log.info("─── Phase 2: Remove Data Directories ───")
    total_data_freed = 0
    for path in DATA_DIRS:
        total_data_freed += remove_directory(path, dry_run)

    log.info("Data directories: %.2f GB", total_data_freed / (1024 ** 3))

    # Phase 3: Remove leftover program directories
    log.info("")
    log.info("─── Phase 3: Remove Leftover Program Directories ───")
    total_prog_freed = 0
    for path in FORCE_REMOVE_DIRS:
        total_prog_freed += remove_directory(path, dry_run)

    log.info("Program directories: %.2f GB", total_prog_freed / (1024 ** 3))

    # Phase 4: Clean PATH
    log.info("")
    log.info("─── Phase 4: Clean PATH ───")
    path_cleaned = clean_path(dry_run)
    log.info("PATH entries to remove: %d", path_cleaned)

    # Phase 5: Clean Registry
    log.info("")
    log.info("─── Phase 5: Clean Registry ───")
    reg_cleaned = clean_registry(dry_run)
    log.info("Registry entries to remove: %d", reg_cleaned)

    # Summary
    total_freed = total_data_freed + total_prog_freed
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY (%s)", mode)
    log.info("=" * 60)
    log.info("  Programs uninstalled: %d", uninstall_count)
    log.info("  Directories removed:  %d", len(DATA_DIRS) + len(FORCE_REMOVE_DIRS))
    log.info("  Space freed (dirs):   %.2f GB", total_freed / (1024 ** 3))
    log.info("  PATH entries cleaned: %d", path_cleaned)
    log.info("  Registry cleaned:     %d", reg_cleaned)
    log.info("")

    if dry_run:
        log.info("This was a DRY RUN. No changes were made.")
        log.info("To execute: python disk_cleanup.py --execute")
    else:
        log.info("Cleanup complete. Restart your computer to finalize.")
        log.info("")
        log.info("After restart, verify:")
        log.info("  1. docker --version")
        log.info("  2. kubectl version --client")
        log.info("  3. python --version")
        log.info("  4. node --version")
        log.info("  5. func --version (Azure Functions — may need .NET Runtime reinstall)")

    log.info("")
    log.info("Full log: %s", LOG_FILE)


if __name__ == "__main__":
    main()
