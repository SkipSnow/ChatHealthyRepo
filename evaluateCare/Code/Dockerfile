# EvaluateCare backend Dockerfile.
#
# Build context: REPO ROOT.
# Build invocation:
#   docker build -t ch-evalcare -f evaluateCare/Code/Dockerfile <repo_root>
#
# Container layout (mirrors evaluateCare/Code/ exactly):
#   /app/app.py                     entry point, the single object that imports each feature
#   /app/healthcheck/               /health
#   /app/displayChrome/             /splash, /transfer/to-findcare
#   /app/security/                  /session, /verify-token, /debug/*, session_token model
#   /app/externalInterface/         /evaluate/providers (FindCare → EvaluateCare handoff)

FROM python:3.12-slim

WORKDIR /app

COPY evaluateCare/Code/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY evaluateCare/Code/app.py             /app/app.py
COPY evaluateCare/Code/healthcheck        /app/healthcheck
COPY evaluateCare/Code/displayChrome      /app/displayChrome
COPY evaluateCare/Code/security           /app/security
COPY evaluateCare/Code/externalInterface  /app/externalInterface

ENV PORT=7860 \
    CERTS_DIR=/certs \
    SSL_CERTFILE=/certs/evalcare.crt \
    SSL_KEYFILE=/certs/evalcare.key
EXPOSE 7860
CMD ["python", "app.py"]
