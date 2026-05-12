# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""workflow_generator — generate GitHub Actions deploy YAMLs from one model.

Replaces the hand-maintained `.github/workflows/deploy-findcare-*.yml`
quadruple that has been drifting silently (e.g. the prod-website slot in
`RemoteDeploy.WEBSITE_WORKFLOWS` pointed at the QA yaml, causing prod
deploys to silently no-op).

Public API:
    from Code.deploy.workflow_generator.workflow_generator import WorkflowGenerator
    from Code.deploy.workflow_generator.technical_model import TECHNICAL_MODEL

    gen = WorkflowGenerator(TECHNICAL_MODEL, repo_root=Path(...))
    gen.write_all()      # writes .github/workflows/*.yml
    # or
    yamls = gen.render_all()  # returns {filename: content}
"""
