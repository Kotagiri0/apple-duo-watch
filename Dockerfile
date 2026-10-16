FROM python:3.12-slim

# Standard library only — nothing to install.
WORKDIR /app
COPY watcher.py /app/watcher.py

RUN useradd --create-home --uid 10001 watcher \
 && mkdir -p /data && chown watcher:watcher /data
USER watcher

ENV STATE_FILE=/data/state.json \
    INTERVAL_SECONDS=60 \
    CITY=Dubai \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
ENTRYPOINT ["python", "-I", "/app/watcher.py"]
