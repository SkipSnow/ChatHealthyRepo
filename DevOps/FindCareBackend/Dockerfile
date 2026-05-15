
FROM python:3.12-slim

WORKDIR /app
COPY DevOps/FindCareBackend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY Code/ConversationalUX/FindCareChat/backend /app/Code/ConversationalUX/FindCareChat/backend
COPY Code/Shared /app/Code/Shared
COPY Code/ConversationalUX/ChatHealthyWhoAmIChat/me /app/Code/ConversationalUX/ChatHealthyWhoAmIChat/me
COPY brain /app/brain
COPY FindCare /app/FindCare
COPY FrontEndApplicationLib /app/FrontEndApplicationLib
RUN pip install --no-cache-dir /app/FrontEndApplicationLib

# build_info.json is written into the build context by the deploy
# substrate (local_deploy.py for docker, the GHA workflow's staging
# step for HF). /health reads it at request time so the build number
# reported = the build number this image was built with.
COPY build_info.json /app/build_info.json

WORKDIR /app/Code/ConversationalUX/FindCareChat/backend
ENV PORT=7860     SSL_CERTFILE=/certs/findcare.crt SSL_KEYFILE=/certs/findcare.key
EXPOSE 7860
CMD ["python", "app.py"]
