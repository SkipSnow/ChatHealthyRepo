# SharedServices backend Dockerfile.
#
# Build context: this directory (sharedServices/Code/). Same paths work for
# local builds (where local_deploy.py uses this dir as the context) and for
# the HF Space build (where the GHA workflow flat-rsyncs this dir's
# contents into the Space root, which becomes the context).
#
# Container layout:
#   /app/app.py                     entry point
#   /app/healthcheck/               /health
#   /app/displayChrome/             /splash, /transfer/to-findcare
#   /app/security/                  /session, /verify-token, session_token
#   /app/externalInterface/         placeholder for future external interfaces
#   /app/secretsManager/            /secrets/{key}

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py             /app/app.py
COPY healthcheck        /app/healthcheck
COPY displayChrome      /app/displayChrome
COPY security           /app/security
COPY externalInterface  /app/externalInterface
COPY secretsManager     /app/secretsManager

ENV PORT=7860 \
    CERTS_DIR=/certs \
    SSL_CERTFILE=/certs/shared.crt \
    SSL_KEYFILE=/certs/shared.key
EXPOSE 7860
CMD ["python", "app.py"]
