# The environment the engineering-rule scans run in.
#
# Design V31 section 4.4.3. The scans depend on chathealthy_lib, and until
# now every module found it by walking parent directories for
# ChatHealthyLib/src and inserting it on sys.path. That is a program
# searching the filesystem for its own dependency: it works on the one
# workstation where the tree happens to sit in the right place, and the
# hosted runner proved the point by dying on the import for five straight
# runs. An environment is a deployed fact, not something a module
# manufactures at import time.
#
# Every version here is the one deployment_architecture.json declares for
# target_host_local_devops under runtime.python_packages. The record and
# this file do not each hold an opinion -- the record holds it, and a
# mismatch is a build failure rather than a discrepancy nobody notices.
#
# What this image deliberately does not run: the two authorization gates.
# They put a question on the operator's screen and wait for a person to
# answer it, and a container has no screen to put it on. They stay on the
# host. This image is for the checks that read files and report.

FROM python:3.12-slim

# Read-only work: no compiler, no cache, no root-owned writable tree.
RUN useradd --create-home --uid 10001 scanner

WORKDIR /repo

# The library goes in as a versioned package. The build context is the
# repository root, so this is the source of the version the record pins.
COPY ChatHealthyLib /tmp/ChatHealthyLib

# chathealthy-lib declares its dependencies as ranges (fastapi>=0.110 and
# so on), so an unpinned install resolves to whatever is newest that day and
# two builds a week apart produce two different images. The record names one
# version per package; these are those versions, installed before the library
# so its ranges resolve to them rather than around them.
RUN python -m pip install --no-cache-dir --upgrade pip==25.3 \
 && python -m pip install --no-cache-dir \
      fastapi==0.128.0 \
      cryptography==46.0.5 \
      pydantic==2.12.5 \
      httpx==0.28.1 \
      pymongo==4.16.0 \
      jsonschema==4.26.0 \
      filelock==3.20.3 \
      pydantic-ai-slim==1.93.0 \
      openai==2.36.0 \
      python-dotenv==1.2.1 \
 && python -m pip install --no-cache-dir --no-deps /tmp/ChatHealthyLib \
 && rm -rf /tmp/ChatHealthyLib

# CH_LOG_DESTINATION was assigned at import by eight modules, because the
# chain materialises an application .env that points logging at Mongo and
# devops tooling has no grant to write there. It is the environment's fact,
# so the environment states it.
ENV CH_LOG_DESTINATION=stderr \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER scanner

# The repository is mounted at /repo; the driver reads the staged set from
# stdin or the branch, and writes its verdict to stdout.
ENTRYPOINT ["python", \
  "architecture/EngineeringRuleEnforcement/code/commit_governance_driver.py"]
