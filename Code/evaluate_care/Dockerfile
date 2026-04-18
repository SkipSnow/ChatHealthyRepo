FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# COPY everything the deploy workflow has staged. This includes:
#   app.py (entry point)
#   evaluate_care/ (package)
#   session_token.py and other Code/Shared/*.py copied by the workflow
#   .commit_sha and any other deploy-time artifacts
# Selective COPY previously missed session_token.py — verify_session_token
# could not import and verify always returned False on HF.
COPY . .
EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
