# Stage 1: Build React frontend
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
ENV VITE_API_URL=""
RUN npm run build

# Stage 2: Python backend + static files
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf frontend
COPY --from=frontend /frontend/dist /app/static
EXPOSE 7860
CMD ["python", "main.py"]
