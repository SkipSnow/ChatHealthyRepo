# SharedServices backend Dockerfile.
#
# Build context: REPO ROOT.
# Build invocation:
#   docker build -t ch-sharedsvc -f sharedServices/Code/Dockerfile <repo_root>
#
# Container layout (mirrors sharedServices/Code/ exactly):
#   /app/app.py                     entry point, the single object that imports each feature
#   /app/healthcheck/               /health
#   /app/displayChrome/             /splash, /transfer/to-findcare
#   /app/security/                  /session, /verify-token, session_token model
#   /app/externalInterface/         placeholder for future external interfaces
#   /app/secretsManager/            /secrets/{key}

FROM python:3.12-slim

WORKDIR /app

COPY sharedServices/Code/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY sharedServices/Code/app.py             /app/app.py
COPY sharedServices/Code/healthcheck        /app/healthcheck
COPY sharedServices/Code/displayChrome      /app/displayChrome
COPY sharedServices/Code/security           /app/security
COPY sharedServices/Code/externalInterface  /app/externalInterface
COPY sharedServices/Code/secretsManager     /app/secretsManager

ENV PORT=7860 \
    CERTS_DIR=/certs \
    SSL_CERTFILE=/certs/shared.crt \
    SSL_KEYFILE=/certs/shared.key
EXPOSE 7860
CMD ["python", "app.py"]
