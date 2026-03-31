FROM python:3.12-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        streamripper \
        ffmpeg \
        rsync \
        procps \
        curl \
        libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp: latest binary from GitHub
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY modules/ ./modules/
COPY tools/ ./tools/

RUN mkdir -p /recording /data /library

ENV SR_UI_DB_PATH=/data/streampeg.db
ENV RUNNING_IN_DOCKER=1
ENV SR_RECORDING_BASE=/recording
ENV SR_LIBRARY_PATH=/library
ENV SR_UI_SECRET=streampeg-secret-change-me
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

VOLUME ["/data", "/recording", "/library"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

CMD ["python3", "app.py"]
