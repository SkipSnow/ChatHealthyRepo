# FindCare backend Dockerfile.
#
# Build context: REPO ROOT (chosen pattern, V11 EPIC-008-F-004
# S-002-REQ-T-001 + BUG-003 #1). All three local backend Dockerfiles use
# the same expand-build-root pattern so docker build can COPY sibling-tree
# paths (Code/Shared/, brain/, Code/ConversationalUX/ChatHealthyWhoAmIChat/me/)
# that live OUTSIDE this directory.
#
# Per Docker docs (https://docs.docker.com/build/concepts/context/):
# "Build instructions such as COPY and ADD can refer to any of the files
#  and directories in the context." — so the context must include every
# path the COPY instructions reference. A narrow context rooted at this
# directory cannot see ../../../Shared or ../../../../brain, which is why
# the previous build crashed with ModuleNotFoundError on
# ChatHealthyMongoUtilities at runtime.
#
# Build invocation (from Code/deploy/local_deploy.py):
#   docker build -t ch-findcare \
#                -f Code/ConversationalUX/FindCareChat/backend/Dockerfile \
#                <repo_root>
# A repo-root .dockerignore keeps the transferred context small.

FROM python:3.12-slim

# Install requirements first so layer caching survives source edits.
WORKDIR /app
COPY Code/ConversationalUX/FindCareChat/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Recreate the host's directory tree inside the container so the application
# code's relative-path lookups in app.py resolve unmodified:
#   sys.path.insert(0, backend/../../../Shared)              -> /app/Code/Shared
#   _brain_dir = backend/../../../../brain                   -> /app/brain
#   _ME_DIR = backend/../../ChatHealthyWhoAmIChat/me         -> /app/Code/ConversationalUX/ChatHealthyWhoAmIChat/me
#   _ops_dir = backend/../../../Shared/ops                   -> /app/Code/Shared/ops
COPY Code/ConversationalUX/FindCareChat/backend /app/Code/ConversationalUX/FindCareChat/backend
COPY Code/Shared /app/Code/Shared
COPY Code/ConversationalUX/ChatHealthyWhoAmIChat/me /app/Code/ConversationalUX/ChatHealthyWhoAmIChat/me
COPY brain /app/brain
# findCare: per-feature canonical home for the V5 SpecialtyFilter tool
# (find_specialists). Backend imports via a sys.path insertion in app.py.
COPY findCare /app/findCare
# EPIC-003: install chathealthy-frontend-lib from the bundled source.
COPY FrontEndApplicationLib /app/FrontEndApplicationLib
RUN pip install --no-cache-dir /app/FrontEndApplicationLib

# Run as the host process would: WORKDIR == backend/, exec `python app.py`.
# app.py's __main__ block reads SSL_CERTFILE/SSL_KEYFILE/PORT env to bind
# uvicorn over HTTPS, matching the host-port HTTPS contract that
# LocalDeploy._wait_for_all_components / _verify_components probe.
WORKDIR /app/Code/ConversationalUX/FindCareChat/backend
ENV PORT=7860 \
    SSL_CERTFILE=/certs/findcare.crt \
    SSL_KEYFILE=/certs/findcare.key
EXPOSE 7860
CMD ["python", "app.py"]
