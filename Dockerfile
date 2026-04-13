FROM python:3.12-slim-bookworm

# System packages: audio tools, stream recording, file sync
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        streamripper \
        ffmpeg \
        rsync \
        procps \
        curl \
        util-linux \
        libchromaprint-tools \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp: latest binary from GitHub (YouTube/SoundCloud downloads)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

# Python dependencies (aubio needs gcc + dev headers to build)
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev pkg-config libffi-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc python3-dev pkg-config libffi-dev && \
    apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Application code
COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY modules/ ./modules/
COPY tools/ ./tools/

# Create required directories
RUN mkdir -p /recording /data /library

# Environment defaults — override via docker-compose or -e flags
ENV SR_UI_DB_PATH=/data/streampeg.db \
    RUNNING_IN_DOCKER=1 \
    SR_RECORDING_BASE=/recording \
    SR_LIBRARY_PATH=/library \
    SR_UI_SECRET=streampeg-secret-change-me \
    SR_UI_PASSWORD=streaming \
    PYTHONUNBUFFERED=1

# Web UI + SlimProto + LMS compat + DLNA
EXPOSE 5000 3483 9000 9090 9091

VOLUME ["/data", "/recording", "/library"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

CMD ["python3", "app.py"]
