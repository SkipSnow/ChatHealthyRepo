# EvaluateCare backend Dockerfile.
#
# Build context: this directory (evaluateCare/Code/). Same paths work for
# local builds (where local_deploy.py uses this dir as the context) and for
# the HF Space build (where the GHA workflow flat-rsyncs this dir's
# contents into the Space root, which becomes the context).
#
# Container layout:
#   /app/app.py                     entry point
#   /app/healthcheck/               /health
#   /app/displayChrome/             /splash, /transfer/to-findcare
#   /app/security/                  /session, /verify-token, /debug/*, session_token
#   /app/externalInterface/         /evaluate/providers (FindCare → EvaluateCare handoff)
#   /app/runtimeGovernance/         ChatHealthyFatalError + register_fatal_handler
#                                   (EPIC-008-F-011-S-001-REQ-B-002 / REQ-B-003)

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py             /app/app.py
COPY healthcheck        /app/healthcheck
COPY displayChrome      /app/displayChrome
COPY security           /app/security
COPY externalInterface  /app/externalInterface
COPY runtimeGovernance  /app/runtimeGovernance

ENV PORT=7860 \
    CERTS_DIR=/certs \
    SSL_CERTFILE=/certs/evalcare.crt \
    SSL_KEYFILE=/certs/evalcare.key
EXPOSE 7860
CMD ["python", "app.py"]
