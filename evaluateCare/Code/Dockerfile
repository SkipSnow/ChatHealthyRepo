
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY FrontEndApplicationLib /app/FrontEndApplicationLib
RUN pip install --no-cache-dir /app/FrontEndApplicationLib

# build_info.json baked at build time; /health reads it.
COPY build_info.json    /app/build_info.json
COPY app.py             /app/app.py
COPY healthcheck        /app/healthcheck
COPY displayChrome      /app/displayChrome
COPY security           /app/security
COPY externalInterface  /app/externalInterface

ENV PORT=7860     CERTS_DIR=/certs     SSL_CERTFILE=/certs/evalcare.crt SSL_KEYFILE=/certs/evalcare.key
EXPOSE 7860
CMD ["python", "app.py"]
