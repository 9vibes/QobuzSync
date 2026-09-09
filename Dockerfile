FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QOBUZ_SYNC_DATA_DIR=/data \
    QOBUZ_SYNC_BACKGROUND=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=23809

WORKDIR /app

RUN addgroup --gid 1000 app && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . && \
    mkdir -p /data /downloads && \
    chown -R app:app /data /downloads /app

VOLUME ["/data", "/downloads"]
EXPOSE 23809

CMD ["python", "-m", "qobuz_sync"]
