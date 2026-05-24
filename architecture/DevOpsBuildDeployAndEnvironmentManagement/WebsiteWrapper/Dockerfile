FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx
# Build context is the repo root; reach the canonical _start_website.py
# in architecture/DevOpsBuildDeployAndEnvironmentManagement/.
COPY architecture/DevOpsBuildDeployAndEnvironmentManagement/_start_website.py /app/_start_website.py
EXPOSE 80
EXPOSE 443
# Run: docker run -v <certs>:/certs:ro -v <website-staging>:/website:ro \
#                 -p 80:80 -p 443:443 ch-website
ENTRYPOINT ["python", "/app/_start_website.py", "/certs", "/website"]
