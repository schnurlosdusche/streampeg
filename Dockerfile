FROM python:3.12-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        streamripper \
        ffmpeg \
        rsync \
        procps \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py db.py auth.py process_manager.py sync.py \
     scheduler.py ffmpeg_recorder.py cleanup.py ./
COPY templates/ ./templates/
COPY static/ ./static/

RUN mkdir -p /recording /data

EXPOSE 5000

CMD ["python3", "app.py"]
