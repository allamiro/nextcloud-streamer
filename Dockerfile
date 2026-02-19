FROM python:3.12-slim

WORKDIR /app

# Install only what we need
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache

COPY gitlab_poller.py .
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh && \
    mkdir -p /data

# Security: non-root user
RUN useradd --uid 1000 --create-home appuser && \
    chown -R appuser:appuser /app /data

USER appuser

ENV STATE_FILE=/data/gitlab_state.json \
    PYTHONUNBUFFERED=1 \
    POLL_INTERVAL=300   # seconds (5 min default)

VOLUME ["/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
