The boot class in `Code/Shared/ops/tools/chathealthy_devops_boot.py` governs your session. It runs deterministically on every hook event via `.claude/settings.json`. Follow its output.

## v4-028: Bug/Feature Governance (MANDATORY)

When the user reports a bug or requests a feature/enhancement, you MUST do the following BEFORE writing any code:

1. Search `brain/machine_artifacts/content/bugs.json` for duplicate or related bugs
2. Search `brain/machine_artifacts/content/agile_backlog.json` for duplicate or contradicting features/requirements
3. If duplicate found: show it to the user, ask whether to update existing or create new
4. If contradiction found: work with the user to resolve it — dedup, edit, or deprecate
5. Only after the requirement/bug is clean and non-contradictory may you begin coding

This rule exists because Claude repeatedly lost track of bugs and requirements, coded fixes that contradicted existing requirements, and created duplicate entries. The user's time is the most expensive resource.
