# EvaluateCare backend Dockerfile.
#
# Build context: REPO ROOT (consistent with the other two local backend
# Dockerfiles, V11 EPIC-008-F-004 S-002-REQ-T-001 + BUG-003 #1).
#
# Why repo root and not Code/evaluate_care/: the previous narrow context
# built an image where /app contained the contents of evaluate_care/ at the
# top level, so `from evaluate_care.models import ScoringRequest` failed
# with ModuleNotFoundError — the package wasn't on PYTHONPATH because the
# package directory itself was the working directory. Per Docker docs
# (https://docs.docker.com/build/concepts/context/): "Build instructions
# such as COPY and ADD can refer to any of the files and directories in the
# context." Expanding the context lets us recreate the on-host package
# layout (app.py at /app/, evaluate_care/ as a sibling subdirectory) so the
# package-style import resolves with /app on sys.path.
#
# The HF Space deploy workflow uses the same flat shape (app.py + evaluate_care/
# at hf_space root) — see .github/workflows/deploy-evaluatecare-backend.yml
# lines 92-104.
#
# Build invocation (from Code/deploy/local_deploy.py):
#   docker build -t ch-evalcare -f Code/evaluate_care/Dockerfile <repo_root>

FROM python:3.12-slim

WORKDIR /app
COPY Code/evaluate_care/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Layout inside the container, mirroring HF Space (so a single set of
# import statements works in both environments):
#   /app/app.py                  — entry point
#   /app/evaluate_care/...       — the evaluate_care package (models.py etc.)
#   /app/<shared *.py>           — Code/Shared/*.py copied flat so
#                                  `from session_token import ...` resolves
COPY Code/evaluate_care/app.py /app/app.py
COPY Code/evaluate_care /app/evaluate_care
# Strip the duplicates of app.py/Dockerfile/requirements.txt that the
# preceding line copied into /app/evaluate_care (they belong only at /app).
RUN rm -f /app/evaluate_care/app.py \
          /app/evaluate_care/Dockerfile \
          /app/evaluate_care/requirements.txt

# Shared utilities — flat copy at /app so package-less imports resolve.
# Matches the HF Space layout produced by deploy-evaluatecare-backend.yml.
COPY Code/Shared/*.py /app/

# Run via __main__ so the SSL block in app.py picks up SSL_CERTFILE/KEYFILE
# and binds uvicorn over HTTPS (matches host-port HTTPS contract).
ENV PORT=7860 \
    SSL_CERTFILE=/certs/evalcare.crt \
    SSL_KEYFILE=/certs/evalcare.key
EXPOSE 7860
CMD ["python", "app.py"]
