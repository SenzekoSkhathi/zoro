# Dockerfile for Zoro 24/7 Cloud Core on Azure Container Apps
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies for audio decoding, git operations, and networking
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ffmpeg \
    libasound2 \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Configure git identity and safe directory
RUN git config --global user.name "Zoro Autonomous Core" && \
    git config --global user.email "zoro@lumen-ai.co.za" && \
    git config --global init.defaultBranch main && \
    git config --global --add safe.directory /app

# Install python dependencies for Cloud Core
COPY requirements-server.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements-server.txt

# Copy application source code
COPY src/ ./src/
COPY data/ ./data/
COPY .gitignore ./

# Initialize local git repository so branch isolation and git status checks are clean
RUN git init && \
    git add -A && \
    git commit -m "init: zoro cloud core baseline"

# Default entrypoint: Run LiveKit worker agent
CMD ["python", "src/zoro_agent.py", "start"]

