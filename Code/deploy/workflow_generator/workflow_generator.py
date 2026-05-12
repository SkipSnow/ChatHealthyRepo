# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""workflow_generator.py — render GitHub Actions deploy YAMLs from one model.

In scope (the four FindCare-related workflows that have been drifting):
  - .github/workflows/deploy-findcare-backend.yml
  - .github/workflows/deploy-findcare-website-dev.yml
  - .github/workflows/deploy-findcare-website-qa.yml
  - .github/workflows/deploy-findcare-website-prod.yml

Design choices:
  1. String-template rendering, NOT yaml.dump. YAML round-trip via pyyaml
     loses the block-scalar formatting and exact whitespace of the
     hand-authored files, which would produce a noisy diff that hides real
     changes. pyyaml IS used to parse the rendered output as a syntax
     sanity check before write (loud failure on syntax errors).
  2. Deterministic — given the same TECHNICAL_MODEL, the output is byte-
     identical run-to-run.
  3. No silent fallbacks — KeyError on missing config; no defaults.
  4. The cross-env backend workflow uses runtime `${BRANCH}` derivation
     (same shape as the existing YAML); the website workflow fans out
     into three files at generation time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml  # parse-only, for syntax sanity check


# ── Website template (per-env) ─────────────────────────────────────────────
# Three substitutions: {display_name}, {branch}, {env}, {cf_project},
# {self_path} (= filename), {fc_url}, {ec_url}, {sh_url},
# {smoke_pip_packages}, {smoke_test_path}.
_WEBSITE_TEMPLATE = """\
name: {display_name}

on:
  workflow_dispatch:
  push:
    branches: [{branch}]
    paths:
      - 'Website/**'
      - '.github/workflows/{self_path}'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '{python_version}'

      # Build-counter bump removed per Rule-063 + BUG-001:
      # the build is now incremented by a post-commit engineering rule on
      # every non-deploy commit, and deploys do NOT bump.

      - name: Create Pages project (idempotent)
        uses: cloudflare/wrangler-action@v3
        with:
          apiToken: ${{{{ secrets.CLOUDFLARE_ACCOUN_TOKEN }}}}
          accountId: ${{{{ secrets.CLOUDFLARE_ACCOUNT_ID }}}}
          command: pages project create {cf_project} --production-branch {branch}
        continue-on-error: true

      - name: Deploy to Cloudflare Pages
        uses: cloudflare/wrangler-action@v3
        with:
          apiToken: ${{{{ secrets.CLOUDFLARE_ACCOUN_TOKEN }}}}
          accountId: ${{{{ secrets.CLOUDFLARE_ACCOUNT_ID }}}}
          command: pages deploy Website --project-name={cf_project} --branch={branch}

      # Poll backend HF Space /health endpoints until 200 before running
      # smoke. If a backend is paused or unhealthy, fail loudly here rather
      # than letting smoke produce a non-deterministic failure pattern.
      - name: Poll backend health before smoke
        run: |
          for URL in \\
              "{fc_url}" \\
              "{ec_url}" \\
              "{sh_url}"; do
            echo "Polling ${{URL}}/health..."
            for i in $(seq 1 30); do
              HTTP_CODE=$(curl -s -o /tmp/h.json -w "%{{http_code}}" -X POST "${{URL}}/health" -m 8)
              if [ "$HTTP_CODE" = "200" ]; then
                echo "  ${{URL}}/health 200 (poll $i)"
                break
              fi
              echo "  poll $i/30: http=$HTTP_CODE"
              if [ $i -eq 30 ]; then
                echo "TIMED OUT waiting for ${{URL}}/health (last: $HTTP_CODE)"
                cat /tmp/h.json
                exit 1
              fi
              sleep 10
            done
          done

      # EPIC-008-F-004-S-001-REQ-B-004: deploy script invokes the master
      # smoke test (S-006) against the target env after the deploy.
      - name: Install smoke test dependencies
        run: |
          pip install --quiet {smoke_pip_packages}
          playwright install --with-deps chromium

      - name: Run master smoke test against deployed env
        env:
          SMOKE_TEST_ENV: {env}
          CHATHEALTHY_PROJECT_ROOT: ${{{{ github.workspace }}}}
        run: |
          # Smoke runs against the env we just deployed to. No deselects,
          # no marker filters per "100% comply" directive (2026-05-04).
          python -m pytest {smoke_test_path} -v
"""


# ── FindCare backend template (cross-env, single file) ─────────────────────
# This template is closer to the existing deploy-findcare-backend.yml; the
# `BRANCH` / `ENV` derivation stays at runtime inside the workflow because
# one workflow services dev/qa/main pushes.
_FINDCARE_BACKEND_TEMPLATE = """\
name: {display_name}

on:
  workflow_dispatch:
  push:
    branches:
      - dev
      - qa
      - main
    paths:
{trigger_paths_yaml}

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 1

      - name: Derive Space name and env from branch
        id: space
        run: |
          BRANCH="${{GITHUB_REF_NAME}}"
          ORG="{org}"
          BASE="{hf_base}"
          if [ "$BRANCH" = "main" ]; then
            SPACE="${{BASE}}"
            ENV="prod"
          else
            SPACE="${{BRANCH}}_${{BASE}}"
            ENV="${{BRANCH}}"
          fi
          echo "name=${{SPACE}}" >> $GITHUB_OUTPUT
          echo "env=${{ENV}}" >> $GITHUB_OUTPUT
          echo "Deploying to: ${{ORG}}/${{SPACE}} (env=${{ENV}})"

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '{python_version}'

      - name: Build React frontend
        uses: actions/setup-node@v4
        with:
          node-version: {node_version}

      - name: Install and build frontend
        working-directory: Code/ConversationalUX/FindCareChat/frontend
        env:
          BRANCH: ${{{{ github.ref_name }}}}
        run: |
          npm ci
          if [ "$BRANCH" = "main" ]; then
            EVALCARE_URL="https://skipsnow-evaluatecarespace.hf.space"
          else
            EVALCARE_URL="https://skipsnow-${{BRANCH}}-evaluatecarespace.hf.space"
          fi
          VITE_API_URL="" VITE_EVALCARE_URL="$EVALCARE_URL" npm run build

      - name: Set HF Space variables + secrets (peer URLs, env, certs, signing key)
        env:
          HF_TOKEN: ${{{{ secrets.HF_TOKEN }}}}
          HF_SPACE: ${{{{ steps.space.outputs.name }}}}
          ENV: ${{{{ steps.space.outputs.env }}}}
          # Private signing key sourced from GH repo secret (one value used
          # across dev/qa/prod — pre-alpha, 2026-05-04). Required for
          # FINDCARE_SIGNING_KEY_PEM bootstrap at container start; without it
          # _startup_security_verification abends with exit=78 (see
          # EPIC-002-F-001-S-012-REQ-T-004).
          FINDCARE_SIGNING_KEY_B64: ${{{{ secrets.FINDCARE_SIGNING_KEY_B64 }}}}
        run: |
          # Derive per-env peer URLs. Prod has no prefix; dev/qa prepend env.
          if [ "$ENV" = "prod" ]; then PREFIX=""; else PREFIX="${{ENV}}-"; fi
          EVALCARE_URL="https://skipsnow-${{PREFIX}}evaluatecarespace.hf.space"
          SHARED_URL="https://skipsnow-${{PREFIX}}sharedservicesspace.hf.space"
          set_var() {{
            K="$1"; V="$2"
            # Delete any pre-existing secret with the same name — HF rejects
            # collision between a variable and a secret, causing CONFIG_ERROR.
            curl -s -X DELETE "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}/secrets" \\
              -H "Authorization: Bearer $HF_TOKEN" \\
              -H "Content-Type: application/json" \\
              -d "{{\\"key\\":\\"${{K}}\\"}}" -o /dev/null
            # Now set the variable (authoritative per-env value from this workflow).
            curl -sf -X POST "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}/variables" \\
              -H "Authorization: Bearer $HF_TOKEN" \\
              -H "Content-Type: application/json" \\
              -d "{{\\"key\\":\\"${{K}}\\",\\"value\\":\\"${{V}}\\",\\"description\\":\\"Set by deploy workflow\\"}}" \\
              -o /dev/null && echo "  set var ${{K}}"
          }}
          set_secret() {{
            # Mirror of set_var but POSTs to /secrets. For values that must
            # NOT be plaintext-visible in HF Space settings (private keys).
            # MUST delete BOTH a same-named variable AND a same-named secret
            # before POSTing — HF rejects var/secret collision AND rejects
            # POST to /secrets if a secret of that name already exists.
            #
            # Build the JSON body via python -c rather than bash -d so a
            # 2KB+ value with shell-special chars (none in base64, but
            # belt+suspenders) cannot break the request shape.
            K="$1"; V="$2"
            curl -s -X DELETE "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}/variables" \\
              -H "Authorization: Bearer $HF_TOKEN" \\
              -H "Content-Type: application/json" \\
              -d "{{\\"key\\":\\"${{K}}\\"}}" -o /dev/null
            curl -s -X DELETE "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}/secrets" \\
              -H "Authorization: Bearer $HF_TOKEN" \\
              -H "Content-Type: application/json" \\
              -d "{{\\"key\\":\\"${{K}}\\"}}" -o /dev/null
            BODY=$(python3 -c "import json,sys; print(json.dumps({{'key':sys.argv[1],'value':sys.argv[2],'description':'Set by deploy workflow'}}))" "$K" "$V")
            HTTP=$(curl -s -X POST "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}/secrets" \\
              -H "Authorization: Bearer $HF_TOKEN" \\
              -H "Content-Type: application/json" \\
              --data "$BODY" \\
              -o /tmp/hf_resp.txt -w "%{{http_code}}")
            if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
              echo "  set secret ${{K}} (http=$HTTP)"
            else
              echo "  ERROR set secret ${{K}}: http=$HTTP"
              echo "  response body:"
              cat /tmp/hf_resp.txt
              echo
              echo "  body length sent: ${{#BODY}}"
              echo "  value length: ${{#V}}"
              return 1
            fi
          }}
          set_var "ENV_PREFIX"          "${{ENV}}"
          set_var "EVALCARE_URL"        "${{EVALCARE_URL}}"
          set_var "SHARED_SERVICES_URL" "${{SHARED_URL}}"

          # Public certs — bootstrap material for token-level mutual auth
          # (EPIC-002-F-001-S-012-REQ-T-002). FindCare needs:
          #   - its own findcare.crt (FINDCARE_CERT_PEM) — peers verify
          #     FindCare-signed tokens against this
          #   - peer certs (SHARED_CERT_PEM, EVALCARE_CERT_PEM) — FindCare
          #     verifies peer-signed tokens against these
          #   - the CA cert (CA_CERT_PEM) — chain verification
          # All .crt files are public (gitignore only excludes .key) so we
          # read them directly from the repo and ship as HF Space variables.
          FINDCARE_CERT_PEM_B64=$(base64 -w 0 Code/Shared/ops/certs/findcare.crt)
          SHARED_CERT_PEM_B64=$(base64 -w 0 Code/Shared/ops/certs/shared.crt)
          EVALCARE_CERT_PEM_B64=$(base64 -w 0 Code/Shared/ops/certs/evalcare.crt)
          CA_CERT_PEM_B64=$(base64 -w 0 Code/Shared/ops/certs/ca.crt)
          set_var "FINDCARE_CERT_PEM" "${{FINDCARE_CERT_PEM_B64}}"
          set_var "SHARED_CERT_PEM"   "${{SHARED_CERT_PEM_B64}}"
          set_var "EVALCARE_CERT_PEM" "${{EVALCARE_CERT_PEM_B64}}"
          set_var "CA_CERT_PEM"       "${{CA_CERT_PEM_B64}}"

          # Private signing key — secret, sourced from GH repo secret.
          # Fail loudly if the GH secret was never created.
          if [ -z "$FINDCARE_SIGNING_KEY_B64" ]; then
            echo "ERROR: GH repo secret FINDCARE_SIGNING_KEY_B64 is empty or unset."
            echo "Create it at https://github.com/{org}/{repo}/settings/secrets/actions"
            echo "Value: base64 -w0 of Code/Shared/ops/certs/findcare.key"
            exit 1
          fi
          set_secret "FINDCARE_SIGNING_KEY_PEM" "${{FINDCARE_SIGNING_KEY_B64}}"

      - name: Push to HuggingFace Space
        env:
          HF_TOKEN: ${{{{ secrets.HF_TOKEN }}}}
          HF_SPACE: ${{{{ steps.space.outputs.name }}}}
        run: |
          git config --global user.email "{git_user_email}"
          git config --global user.name "{git_user_name}"
          git lfs install

          echo "Target Space: {org}/${{HF_SPACE}}"
          git clone https://{org}:$HF_TOKEN@huggingface.co/spaces/{org}/${{HF_SPACE}} hf_space

          # Preserve README.md (HF Space config frontmatter)
          cp hf_space/README.md /tmp/hf_readme.md 2>/dev/null || true
          find hf_space -mindepth 1 -not -path 'hf_space/.git*' -delete
          cp /tmp/hf_readme.md hf_space/README.md 2>/dev/null || true

          # Recreate the repo tree under hf_space/ so the FindCare backend
          # Dockerfile's COPY paths (Code/Shared, brain, etc.) resolve. The
          # Dockerfile was authored for the local build (context = repo root)
          # and app.py's relative imports (sys.path.insert(0, ../../../Shared)
          # etc.) work unmodified when /app mirrors the host tree.

          mkdir -p hf_space/Code/ConversationalUX/FindCareChat/backend
          rsync -av \\
            --exclude='.env' \\
            --exclude='__pycache__' \\
            --exclude='*.pyc' \\
            Code/ConversationalUX/FindCareChat/backend/ hf_space/Code/ConversationalUX/FindCareChat/backend/

          # Pre-built React frontend → backend/static (where app.py mounts it)
          cp -r Code/ConversationalUX/FindCareChat/frontend/dist hf_space/Code/ConversationalUX/FindCareChat/backend/static

          # Code/Shared/ — preserve subdirs (ops/, agent_framework/, ux/, tests/)
          mkdir -p hf_space/Code/Shared
          rsync -av \\
            --exclude='__pycache__' \\
            --exclude='*.pyc' \\
            Code/Shared/ hf_space/Code/Shared/

          # PKI certs deferred to Beta — HF doesn't support mTLS

          # Brain artifacts needed at runtime — copy the entire content/ dir so
          # any new JSON the backend reads (prompts.json for SpecialtyFilter,
          # emergency_keywords.json for SafetyService, future records) is
          # staged automatically. Cherry-picking individual files is how
          # prompts.json was missed when SpecialtyFilter wired it 2026-05-04;
          # the FileNotFoundError surfaced post-deploy instead of pre-deploy.
          mkdir -p hf_space/brain/machine_artifacts/content
          rsync -av brain/machine_artifacts/content/ hf_space/brain/machine_artifacts/content/

          # me/ context files (PDFs, summary, codebase)
          mkdir -p hf_space/Code/ConversationalUX/ChatHealthyWhoAmIChat/me
          rsync -av Code/ConversationalUX/ChatHealthyWhoAmIChat/me/ hf_space/Code/ConversationalUX/ChatHealthyWhoAmIChat/me/

          # EPIC-003: bring FrontEndApplicationLib into the build context so the
          # Dockerfile can pip-install chathealthy-frontend-lib from a local path.
          mkdir -p hf_space/FrontEndApplicationLib
          rsync -av \\
            --exclude='__pycache__' \\
            --exclude='*.pyc' \\
            FrontEndApplicationLib/ hf_space/FrontEndApplicationLib/

          # Dockerfile lives at the build-context root for HF.
          cp Code/ConversationalUX/FindCareChat/backend/Dockerfile hf_space/Dockerfile

          cd hf_space
          git lfs track "*.pdf"
          git add .
          if git diff --staged --quiet; then
            echo "No changes to deploy."
          else
            git commit -m "Deploy from GitHub commit ${{{{ github.sha }}}}"
            git push
            echo "Deployed successfully."
          fi

      # EPIC-008-F-004-S-001-REQ-B-003: build/start/verify gate.
      # HF Spaces rebuild asynchronously after the git push above. Poll
      # the HF API for runtime.stage = RUNNING, then poll /health on each
      # of the three backends until 200.
      - name: Wait for HF Space rebuild + verify /health on all three backends
        env:
          HF_TOKEN: ${{{{ secrets.HF_TOKEN }}}}
          HF_SPACE: ${{{{ steps.space.outputs.name }}}}
          ENV: ${{{{ steps.space.outputs.env }}}}
        run: |
          if [ "$ENV" = "prod" ]; then
            FINDCARE_URL="https://skipsnow-chathealthyspace.hf.space"
            EVALCARE_URL="https://skipsnow-evaluatecarespace.hf.space"
            SHARED_URL="https://skipsnow-sharedservicesspace.hf.space"
          else
            FINDCARE_URL="https://skipsnow-${{ENV}}-chathealthyspace.hf.space"
            EVALCARE_URL="https://skipsnow-${{ENV}}-evaluatecarespace.hf.space"
            SHARED_URL="https://skipsnow-${{ENV}}-sharedservicesspace.hf.space"
          fi
          echo "Waiting for HF Space {org}/${{HF_SPACE}} to reach RUNNING..."
          for i in $(seq 1 60); do
            STAGE=$(curl -s -H "Authorization: Bearer $HF_TOKEN" \\
              "https://huggingface.co/api/spaces/{org}/${{HF_SPACE}}" \\
              | jq -r '.runtime.stage // ""')
            echo "  poll $i/60: stage=$STAGE"
            if [ "$STAGE" = "RUNNING" ]; then break; fi
            if [ "$STAGE" = "RUNTIME_ERROR" ] || [ "$STAGE" = "BUILD_ERROR" ]; then
              echo "HF Space failed: stage=$STAGE"; exit 1
            fi
            if [ $i -eq 60 ]; then
              echo "TIMED OUT after 10 min (final stage: $STAGE)"; exit 1
            fi
            sleep 10
          done
          for URL in "$FINDCARE_URL" "$EVALCARE_URL" "$SHARED_URL"; do
            echo "Polling ${{URL}}/health..."
            for i in $(seq 1 30); do
              HTTP_CODE=$(curl -s -o /tmp/h.json -w "%{{http_code}}" \\
                -X POST "${{URL}}/health" -m 8)
              if [ "$HTTP_CODE" = "200" ]; then
                echo "  ${{URL}}/health 200 (poll $i):"
                cat /tmp/h.json; echo
                break
              fi
              echo "  poll $i/30: http=$HTTP_CODE"
              if [ $i -eq 30 ]; then
                echo "TIMED OUT waiting for ${{URL}}/health (last: $HTTP_CODE)"
                cat /tmp/h.json
                exit 1
              fi
              sleep 10
            done
          done

      # EPIC-008-F-004-S-001-REQ-B-004: deploy script invokes the master
      # smoke test (S-006) against the target env after build/start/verify.
      - name: Install smoke test dependencies
        run: |
          pip install --quiet {smoke_pip_packages}
          playwright install --with-deps chromium

      - name: Run master smoke test against deployed env
        env:
          SMOKE_TEST_ENV: ${{{{ steps.space.outputs.env }}}}
          CHATHEALTHY_PROJECT_ROOT: ${{{{ github.workspace }}}}
        run: |
          # Smoke runs against the env we just deployed to (dev/qa/prod).
          # No deselects, no marker filters — every test in the master smoke
          # is in scope per the "100% comply" directive (2026-05-04).
          # Tests that have hardcoded localhost assumptions or local-vs-dev
          # build comparisons will fail honestly until they are fixed at
          # the test source, not silenced at the invocation site.
          python -m pytest {smoke_test_path} -v
"""


# ── HF URL helpers ─────────────────────────────────────────────────────────
def _hf_url(env: str, service_base: str) -> str:
    """Public hf.space URL for a service in a given env.

    Prod: skipsnow-{base_lower}.hf.space
    Dev/QA: skipsnow-{env}-{base_lower}.hf.space
    """
    base_lower = service_base.lower()
    if env == "prod":
        return f"https://skipsnow-{base_lower}.hf.space"
    return f"https://skipsnow-{env}-{base_lower}.hf.space"


class WorkflowGenerator:
    """Render the four FindCare-related deploy workflow YAMLs.

    Usage:
        gen = WorkflowGenerator(model, repo_root=Path("c:/CHATHealthyLLC"))
        gen.write_all()      # writes to <repo_root>/.github/workflows/
        # or
        gen.render_all()     # returns {filename: yaml_content_str}
    """

    # In-scope filenames. The other backends (evalcare, shared) follow the
    # same model but their templates are not yet implemented here; the
    # generator's `render_all()` returns only the four listed below.
    IN_SCOPE_FILENAMES = (
        "deploy-findcare-backend.yml",
        "deploy-findcare-website-dev.yml",
        "deploy-findcare-website-qa.yml",
        "deploy-findcare-website-prod.yml",
    )

    def __init__(self, model: Dict, repo_root: Path) -> None:
        self.model = model
        self.repo_root = Path(repo_root).resolve()
        self.workflows_dir = self.repo_root / ".github" / "workflows"
        # Loud validation: every required key must be present.
        for k in (
            "org", "repo", "python_version", "node_version",
            "git_user_email", "git_user_name", "envs",
            "cloudflare_pages_project", "hf_space_base",
            "smoke_test_path", "smoke_pip_packages",
            "backend_workflows", "website_workflow",
        ):
            if k not in self.model:
                raise KeyError(
                    f"technical_model is missing required key {k!r}"
                )
        for env in ("dev", "qa", "prod"):
            if env not in self.model["envs"]:
                raise KeyError(f"envs is missing required env {env!r}")
            if env not in self.model["cloudflare_pages_project"]:
                raise KeyError(
                    f"cloudflare_pages_project is missing env {env!r}"
                )

    # ── Render: website workflow (per env) ────────────────────────────────
    def _render_website(self, env: str) -> str:
        env_cfg = self.model["envs"][env]
        branch = env_cfg["branch"]
        ws = self.model["website_workflow"]
        filename = ws["filename_template"].format(env=env)
        display = ws["display_name_template"].format(
            env_title={"dev": "Dev", "qa": "QA", "prod": "Prod"}[env]
        )
        return _WEBSITE_TEMPLATE.format(
            display_name=display,
            branch=branch,
            env=env,
            cf_project=self.model["cloudflare_pages_project"][env],
            self_path=filename,
            python_version=self.model["python_version"],
            fc_url=_hf_url(env, self.model["hf_space_base"]["findcare"]),
            ec_url=_hf_url(env, self.model["hf_space_base"]["evalcare"]),
            sh_url=_hf_url(env, self.model["hf_space_base"]["shared"]),
            smoke_pip_packages=self.model["smoke_pip_packages"],
            smoke_test_path=self.model["smoke_test_path"],
        )

    # ── Render: findcare backend (cross-env, single file) ─────────────────
    def _render_findcare_backend(self) -> str:
        wf = self.model["backend_workflows"]["findcare_backend"]
        trigger_paths_yaml = "\n".join(
            f"      - '{p}'" for p in wf["trigger_paths"]
        )
        return _FINDCARE_BACKEND_TEMPLATE.format(
            display_name=wf["display_name"],
            trigger_paths_yaml=trigger_paths_yaml,
            org=self.model["org"],
            repo=self.model["repo"],
            hf_base=self.model["hf_space_base"]["findcare"],
            python_version=self.model["python_version"],
            node_version=self.model["node_version"],
            git_user_email=self.model["git_user_email"],
            git_user_name=self.model["git_user_name"],
            smoke_pip_packages=self.model["smoke_pip_packages"],
            smoke_test_path=self.model["smoke_test_path"],
        )

    # ── Public: render all four ───────────────────────────────────────────
    def render_all(self) -> Dict[str, str]:
        """Return {filename: yaml_content} for all four in-scope workflows."""
        out: Dict[str, str] = {}
        out["deploy-findcare-backend.yml"] = self._render_findcare_backend()
        for env in ("dev", "qa", "prod"):
            fn = self.model["website_workflow"]["filename_template"].format(
                env=env
            )
            out[fn] = self._render_website(env)
        # Syntax sanity: every rendered file must parse as YAML.
        for fn, content in out.items():
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                raise RuntimeError(
                    f"Generator produced invalid YAML for {fn}: {e}"
                ) from e
        return out

    # Existing on-disk YAMLs use CRLF line endings (they were authored on
    # Windows and `core.autocrlf` did not normalize them on commit). The
    # generator's templates use Python source `\n`. We translate to CRLF
    # at the byte-write boundary so that:
    #   - generator output is byte-identical to the current on-disk files
    #     (verified by test_workflow_generator)
    #   - swapping in the generator does not generate a noisy "every line
    #     changed" diff that would mask real workflow content changes
    LINE_ENDING = b"\r\n"

    # ── Public: write all four ────────────────────────────────────────────
    def write_all(self) -> Dict[str, Path]:
        """Render all four and write them under <repo_root>/.github/workflows/.

        Returns {filename: absolute_path_written}.
        """
        if not self.workflows_dir.is_dir():
            raise FileNotFoundError(
                f"workflows dir does not exist: {self.workflows_dir}"
            )
        rendered = self.render_all()
        written: Dict[str, Path] = {}
        for fn, content in rendered.items():
            target = self.workflows_dir / fn
            payload = self._encode(content)
            target.write_bytes(payload)
            written[fn] = target
        return written

    @classmethod
    def _encode(cls, content: str) -> bytes:
        """Encode rendered YAML as UTF-8 with CRLF line endings."""
        # str.split keeps a trailing empty string when content ends in "\n";
        # joining with CRLF preserves the trailing-newline shape verbatim.
        lines = content.split("\n")
        return cls.LINE_ENDING.join(s.encode("utf-8") for s in lines)
