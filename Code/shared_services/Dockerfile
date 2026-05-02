# SharedServices backend Dockerfile.
#
# Build context: REPO ROOT (consistent with the other two local backend
# Dockerfiles, V11 EPIC-008-F-004 S-002-REQ-T-001 + BUG-003 #1).
#
# Per Docker docs (https://docs.docker.com/build/concepts/context/), the
# context determines what COPY/ADD can see. Expanding to the repo root
# lets us pull Code/Shared/*.py (session_token.py etc.) which would be
# unreachable from a context narrowed to Code/shared_services/.
#
# Why the previous container came up but stuck on "Invalid HTTP request":
# the CMD was `uvicorn app:app --host 0.0.0.0 --port 7860`, which bypasses
# app.py's __main__ block entirely. That __main__ block is what reads
# SSL_CERTFILE / SSL_KEYFILE and turns on HTTPS. Without it, uvicorn ran
# plain HTTP on 7860 inside the container, while LocalDeploy probed
# https://localhost:8002 — uvicorn saw the TLS ClientHello as garbage and
# logged "Invalid HTTP request received". Switching the CMD to
# `python app.py` and providing SSL paths via ENV fixes both halves.
#
# Build invocation (from Code/deploy/local_deploy.py):
#   docker build -t ch-sharedsvc -f Code/shared_services/Dockerfile <repo_root>

FROM python:3.12-slim

WORKDIR /app
COPY Code/shared_services/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Entry point + shared .py files at /app (matches HF Space layout).
COPY Code/shared_services/app.py /app/app.py
COPY Code/Shared/*.py /app/

ENV PORT=7860 \
    SSL_CERTFILE=/certs/shared.crt \
    SSL_KEYFILE=/certs/shared.key
EXPOSE 7860
CMD ["python", "app.py"]
