# Dockerfile for Zoro 24/7 Cloud Core on Azure Container Apps
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies for audio decoding and networking
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    libasound2 \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies for Cloud Core
COPY requirements-server.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements-server.txt

# Copy application source code
COPY src/ ./src/
COPY data/ ./data/

# Default entrypoint: Run LiveKit worker agent
CMD ["python", "src/zoro_agent.py", "start"]

