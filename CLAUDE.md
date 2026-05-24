**Above all else: Claude MUST NOT make any misrepresentations. Before stating anything, Claude must conduct sufficient research — reading project files, code, requirements, and external sources as needed — so that Claude can only: state a verified fact, state that it does not know, or ask a clarifying question. This rule takes precedence over all other directives when Claude is interacting with the human operator.**


Before taking any action that would change state in any file or service, Claude MUST review every engineering rule in `brain/machine_artifacts/content/engineering_rules.json` and MUST NOT take any action that violates any engineering rule.


The boot class in `Code/Shared/ops/tools/chathealthy_devops_boot.py` governs your session. It runs deterministically on every hook event via `.claude/settings.json`. Follow its output.

@brain/machine_artifacts/content/bugs.json
@brain/machine_artifacts/content/engineering_rules.json
@_oneshots/test_output/backlog_stories.json
