RESTORE TO LAST WORKING STATE — v0.1.4 (2026-04-09)
====================================================

This commit is the last known working state before any further changes.
Claude Code is functional. Hooks fire. Brain is in brainbak/ as a safety measure.

WHAT THIS STATE LOOKS LIKE:
- Branch: dev
- brain/ renamed to brainbak/ (brain JSON files are all here)
- Code/Shared/brain_auth.py renamed to brain_auth.py.bak
- Code/Shared/brain_loop.py renamed to brain_loop.py.bak
- Code/Shared/ops/tools/chathealthy_devops_boot.py has Framework 0.1.3 boot script
- .claude/settings.json has 2 hooks wired (SessionStart, UserPromptSubmit)
- .claude/settings.local.json has permission allowlists (NOT tracked in git)
- Claude Code plugin was reinstalled fresh

TO RESTORE TO THIS STATE FROM GIT BASH:
========================================

Step 1: Check out this commit
    git checkout dev
    git log --oneline -5
    (find the commit titled "last working copy v0.1.4" and copy its hash)
    git checkout <hash>

    OR if you tagged it:
    git checkout last-working-v0.1.4

Step 2: Verify
    ls brainbak/machine_artifacts/content/   (should have ~30 JSON files)
    ls Code/Shared/brain_auth.py.bak         (should exist)
    cat .claude/settings.json                (should show 2 hooks)

Step 3: If Claude Code is broken, reinstall the plugin:
    - VS Code: Ctrl+Shift+P > "Extensions: Install Extension" > search "Claude Code"
    - Or from terminal: code --install-extension anthropic.claude-code

WHAT BROKE LAST TIME (2026-04-09):
===================================
The chathealthy_devops_boot.py hook script was being developed.
It calls GPT-4.1-mini on every user prompt (handle_user_prompt_submit).
If that call hangs or throws, the hook blocks Claude entirely.
The fix was:
1. Rename brain/ to brainbak/ (boot script can't find brain JSONs, fails open)
2. Rename brain_auth.py, brain_loop.py to .bak
3. Reinstall Claude Code plugin

FILES NOT IN GIT (must be restored manually if lost):
=====================================================
- .claude/settings.local.json (permissions — gitignored)
- C:\Users\skips\.claude\ (global Claude config — outside repo)
- Code/.env (environment variables — gitignored)
