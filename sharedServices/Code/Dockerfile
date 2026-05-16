
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY FrontEndApplicationLib /app/FrontEndApplicationLib
RUN pip install --no-cache-dir /app/FrontEndApplicationLib

COPY build_info.json    /app/build_info.json
COPY app.py             /app/app.py
COPY healthcheck        /app/healthcheck
COPY displayChrome      /app/displayChrome
COPY authentication     /app/authentication
COPY externalInterface  /app/externalInterface
COPY secretsManager     /app/secretsManager

ENV PORT=7860     CERTS_DIR=/certs     SSL_CERTFILE=/certs/shared.crt SSL_KEYFILE=/certs/shared.key
EXPOSE 7860
CMD ["python", "app.py"]
