FROM python:3.12-slim

# Faster, cleaner Python in containers
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code
COPY backend/ backend/
COPY frontend/ frontend/

# Data (SQLite + catalog cache) lives on a mounted volume
ENV SHOPALBI_DATA_DIR=/data \
    SHOPALBI_FRONTEND_DIR=/app/frontend \
    SHOPALBI_HOST=0.0.0.0 \
    SHOPALBI_PORT=8000
VOLUME ["/data"]
EXPOSE 8000

WORKDIR /app/backend
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
