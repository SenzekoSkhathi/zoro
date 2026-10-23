# Dockerfile for Zoro 24/7 Cloud Core on Azure Container Apps
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies for audio decoding, git operations, networking, and Node.js LTS
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ffmpeg \
    libasound2 \
    libportaudio2 \
    portaudio19-dev \
    ca-certificates \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
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

# Install web dependencies so frontend builds and boot checks succeed
COPY web/package.json web/package-lock.json* ./web/
RUN cd web && npm install

# Copy application source code (full repository context for autonomous engineering)
COPY src/ ./src/
COPY data/ ./data/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY agents/ ./agents/

# Initialize local git repository so branch isolation, git diffs, and self-mod work cleanly
RUN git init && \
    printf "data/*\n!data/.gitkeep\n*.log\n__pycache__/\nweb/node_modules/\nweb/.next/\n" > .gitignore && \
    git add -A && \
    git commit -m "init: zoro cloud core baseline"

# Default entrypoint: Run LiveKit worker agent
CMD ["python", "src/zoro_agent.py", "start"]
