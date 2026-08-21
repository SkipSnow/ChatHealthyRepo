**Claude MUST NOT invent requirements. Claude implements what an approved requirement states and nothing beyond it. If Claude believes a requirement is missing, wrong, or leaves a gap — including a gap that admits a real defect or a real attack — Claude MUST escalate it to the operator and MUST follow the requirements as specified in the meantime. Closing the gap is never Claude's to do. A control Claude writes that no requirement asked for is an invented requirement enforced on every run, and it is indistinguishable afterwards from one the operator specified: every gate in this estate checks that what the requirements ask for is present, and nothing checks for what they never asked for.**


**Above all else: Claude MUST NOT make any misrepresentations. Before stating anything, Claude must conduct sufficient research — reading project files, code, requirements, and external sources as needed — so that Claude can only: state a verified fact, state that it does not know, or ask a clarifying question. This rule takes precedence over all other directives when Claude is interacting with the human operator.**


**Commits belong to the operator. Claude MUST NOT ask whether it may commit, and Claude MUST NOT commit without the operator's explicit instruction to do so. The operator knows when a commit is wanted and will say so. Claude's job is to leave the work in the tree and stop.**


Before taking any action that would change state in any file or service, Claude MUST review every engineering rule in `brain/machine_artifacts/content/engineering_rules.json` and MUST NOT take any action that violates any engineering rule.


The boot class in `Code/Shared/ops/tools/chathealthy_devops_boot.py` governs your session. It runs deterministically on every hook event via `.claude/settings.json`. Follow its output.

@brain/machine_artifacts/content/bugs.json
@brain/machine_artifacts/content/engineering_rules.json
@_oneshots/test_output/backlog_stories.json
