FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# app.py is the entry point, evaluate_care/ is the package
COPY app.py .
COPY evaluate_care/ evaluate_care/
EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
