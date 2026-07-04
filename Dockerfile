# Pinned to 3.11.9 — matches the rest of the HomeHustleNG stack
# (see the VoxSync lesson: langchain-style deps break on 3.12+, and
# keeping every service on the same version avoids surprises).
FROM python:3.11.9-slim

# ffmpeg + ffprobe are required at runtime for clip detection/trimming.
# Installing them here means Render (or anywhere else) always has them,
# instead of relying on the platform's buildpack having ffmpeg baked in.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so Docker can cache this layer — dependencies
# only get reinstalled when requirements.txt actually changes, not on
# every single code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most platforms) inject $PORT at runtime — shell form lets
# that env var actually get substituted, unlike exec-form CMD arrays.
CMD uvicorn audio_engine:app --host 0.0.0.0 --port ${PORT:-8000}
