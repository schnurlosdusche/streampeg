FROM python:3.12-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        streamripper \
        ffmpeg \
        rsync \
        procps \
        curl \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp (latest binary, updates often)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/

RUN mkdir -p /recording /data

EXPOSE 5000

CMD ["python3", "app.py"]
