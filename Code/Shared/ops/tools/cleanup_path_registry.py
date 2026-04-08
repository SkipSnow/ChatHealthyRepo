#!/usr/bin/env python3
"""
ChatHealthy.ai — PATH + Registry Cleanup Only
Run as Administrator.

Usage: python cleanup_path_registry.py
"""

import os
import sys
import winreg
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("cleanup")

PATH_REMOVE = ["Unity", "Blender", "JetBrains", "PyCharm", "QGIS", "MongoDB",
               "WildTangent", "CoffeeCup", "Ollama", "dotnet"]
PATH_KEEP = ["dotnet\\shared", "npm", "WindowsApps"]


def clean_path():
    for scope, hive, key_path in [
        ("User", winreg.HKEY_CURRENT_USER, r"Environment"),
        ("System", winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]:
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            path_value, _ = winreg.QueryValueEx(key, "Path")
            entries = path_value.split(";")
            original = len(entries)
            new_entries = []
            removed = 0

            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                drop = False
                for term in PATH_REMOVE:
                    if term.lower() in entry.lower():
                        keep = any(k.lower() in entry.lower() for k in PATH_KEEP)
                        if not keep:
                            drop = True
                            break
                if not drop and "%" not in entry and not os.path.exists(entry):
                    drop = True
                if drop:
                    log.info("  %s PATH removing: %s", scope, entry)
                    removed += 1
                else:
                    new_entries.append(entry)

            if removed > 0:
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(new_entries))
                log.info("  %s PATH: %d -> %d entries (%d removed)", scope, original, len(new_entries), removed)
            else:
                log.info("  %s PATH: clean, nothing to remove", scope)
            winreg.CloseKey(key)
        except Exception as e:
            log.error("  %s PATH FAILED: %s", scope, e)


def clean_registry():
    import re
    uninstall_terms = ["Unity", "Blender", "JetBrains", "PyCharm", "QGIS", "MongoDB",
                       "WildTangent", "CoffeeCup", "Ollama", "Visual Studio"]
    keep_terms = ["Visual Studio Code", "Microsoft .NET Runtime", "Microsoft .NET Host",
                  "Docker", "McAfee", "Microsoft Office", "Microsoft Edge"]

    for hive, path in [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]:
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
                        # Check if it's a program we uninstalled and its install location is gone
                        should_remove = False
                        for term in uninstall_terms:
                            if re.search(re.escape(term), name, re.IGNORECASE):
                                keep = any(re.search(k, name, re.IGNORECASE) for k in keep_terms)
                                if not keep:
                                    if install_loc and not os.path.exists(install_loc):
                                        should_remove = True
                                    elif not install_loc:
                                        should_remove = True
                                break
                        if should_remove:
                            to_delete.append((subkey_name, name))
                    except FileNotFoundError:
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except OSError:
                    pass

            for subkey_name, name in to_delete:
                try:
                    winreg.DeleteKey(key, subkey_name)
                    log.info("  Registry removed: %s", name)
                except Exception as e:
                    log.warning("  Registry failed: %s — %s", name, e)

            winreg.CloseKey(key)
            if not to_delete:
                log.info("  Registry hive clean: %s", path.split("\\")[-1])
        except Exception as e:
            log.error("  Registry access FAILED: %s", e)


if __name__ == "__main__":
    log.info("=== PATH + Registry Cleanup (run as Administrator) ===")
    log.info("")
    log.info("--- PATH ---")
    clean_path()
    log.info("")
    log.info("--- Registry ---")
    clean_registry()
    log.info("")
    log.info("Done. No reboot needed.")
