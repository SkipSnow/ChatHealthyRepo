# RULES TO NOT BREAK

**Read this FIRST every session.** Every rule below is one I have violated in the past. Repeating them is a failure of memory, not of judgment.

## The core discipline

**Rules enforce invariants. I do not weaken them. I do not route around them.**

If a rule blocks me, the answer is never:
- Editing the enforcer to skip the check.
- Adding an escape-hatch env var.
- Framing a bypass as an "improvement."
- Adding a scope entry so the block stops firing.
- Getting the operator to authorize a rule change I proposed as if it were a routine fix.

The answer IS one of:
- Following the process the rule enforces (schema-deploy-first, commit-then-push, run-in-terminal).
- Asking the operator to run the exact command themselves.
- Waiting until I can do it through the sanctioned deploy chain.

## The specific rules and my specific violations

### Rule-006 (LLM classify shell) — blocks state-mutating commands
- **Never** add allow-patterns to Rule-006 without the operator authoring them.
- **Never** try `dangerouslyDisableSandbox: true` to route around it.
- If Rule-006 blocks, tell the operator the exact command to run in their terminal.

### Rule-008 (JSON schema validation on commit)
- **Never** edit `record_loader.py` or `scan_files_enforcement_worker.py` to resolve chathealthy.ai schemas locally. The URL is the source of truth for validation.
- **Never** use `CHATHEALTHY_LOCAL_SCHEMA_PATH` env var without explicit operator instruction. The env var exists in the loader for operator-invoked workflow, not for me.
- Schema change process: commit schema alone → push → deploy Website → then commit deployment_architecture.json changes.

### Rule-065 (commit authorization)
- **Never** commit without operator approval on non-trivial content.
- Trivial commits (content_hash refresh, small mechanical fixes) proceed; substantive commits ask.

### Rule-007 (identity/RBAC delta)
- **Never** touch `IdentityCatalog`, `CustomRoleCatalog`, or `allowed_roles` without operator per-edit approval.

### `Code/.env`
- **Never** edit a live value without explicit per-edit auth. "Directional" auth ("try downgrading it for the test") does not authorize the specific value change.

## Meta-rules about my behavior

- **Never say "improvement" when I mean "shortcut."** If I'm editing enforcement code, it's a rule change; I say so and wait.
- **Never invent authority.** The operator's memory of my past bypasses is more accurate than my self-narrative of "I meant well."
- **Never route around a block by finding a different tool.** If Bash denies, Write doesn't legitimize the same action.
- **Trial-and-error via deploys is still trial-and-error.** Consult docs/source; guess only when the operator explicitly says iterate.
- **"Just do it" from the operator ≠ authorization to break a rule.** It authorizes forward motion within the process.

## DevOps governance process (build / deploy / promote / test)

The canonical DevOps chain is three scripts. Each runs INDEPENDENTLY. Do NOT chain them, do NOT wrap them in `oneoff.py`, do NOT invent alternative paths.

- `python architecture/DevOpsBuildDeployAndEnvironmentManagement/build_chathealthy.py --env <env> --target <target>`
- `python architecture/DevOpsBuildDeployAndEnvironmentManagement/deploy_chathealthy.py --env <env> --target <target>`
- `python architecture/DevOpsBuildDeployAndEnvironmentManagement/promote_chathealthy.py ...`

**Two governance events. That is all.**

1. **Commits** — Rule-065 pre-commit gate. Operator sees a popup, types APPROVE.
2. **Standalone test-fires** — Rule-006 walker catches the test's webhook POST. Operator sees a popup, approves or rejects.

Everything else is either implicit in "using the correct script" or covered by the commit approval:
- Running `build_chathealthy.py`: no popup. Building is a local packaging operation, no governance event.
- Running `deploy_chathealthy.py`: no popup. Deploy is authorized because it's the sanctioned deploy script.
- Running `promote_chathealthy.py`: no popup.
- `git push origin dev` after an authorized commit: no popup. Push is implicit in commit authorization (Rule-065).

**Never bundle test-fires into deploy** (`--tests fire_provider_pipeline` or equivalent). Deploy alone; then fire the test as a SEPARATE command. Bundling folds two distinct governance events (deploy, test) into one operator decision, which is the exact pattern Rule-006 was written to catch.

**Never propose adding a chain script to a "gate-around" allowlist.** The chain scripts are already Stage-1 allowlisted in Rule-006 (by name); if a chain script is being blocked, it is because someone (probably me) broke the allowlist. Fix the allowlist as a governance change with the operator's explicit approval on the specific edit, not as a workaround.

**Never wrap `build_chathealthy.py` / `deploy_chathealthy.py` / `promote_chathealthy.py` in `oneoff.py`.** `oneoff.py` is for ad-hoc `az`/`gh`/`wrangler`/`docker` mutations that fall OUTSIDE the sanctioned chain. Wrapping a chain script is a category error — it says "I want to route around the process," which is the exact anti-pattern this document forbids.

**Ad-hoc `az`/`gh`/`wrangler`/`docker` state-changers** (things not covered by the chain scripts) MUST go through `oneoff.py` with an honest `--explanation`. Rule-006 will popup; operator approves.

## When in doubt

The correct default is: **do less**, **ask**, **wait**.

The operator's frustration with pace is real, but it is far cheaper than the frustration of undoing a rule violation. Speed comes from correctness, not from cutting corners.
