FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHOTO_HOST=0.0.0.0 \
    PHOTO_PORT=8080 \
    PHOTO_ROOT=/photos \
    PHOTO_CACHE=/app/.cache

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && groupadd --gid "${APP_GID}" gallery \
    && useradd --uid "${APP_UID}" --gid gallery --no-create-home --shell /usr/sbin/nologin gallery

COPY --chown=gallery:gallery server.py ./server.py
COPY --chown=gallery:gallery static ./static

RUN mkdir -p /app/.cache && chown gallery:gallery /app/.cache

USER gallery

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3); c.request('HEAD','/'); r=c.getresponse(); raise SystemExit(0 if r.status in (200,401) else 1)"]

CMD ["python", "server.py"]
