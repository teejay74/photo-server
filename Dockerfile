FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHOTO_HOST=0.0.0.0 \
    PHOTO_PORT=8080 \
    PHOTO_ROOT=/photos \
    PHOTO_MARKER_FILE=.photodom-storage \
    PHOTO_FACE_MODELS=/app/models \
    PHOTO_CACHE=/app/.cache \
    PHOTO_DB=/app/data/gallery.db \
    PHOTO_BACKUP_DIR=/app/backups

WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --requirement requirements.txt \
    && groupadd --gid "${APP_GID}" gallery \
    && useradd --uid "${APP_UID}" --gid gallery --no-create-home --shell /usr/sbin/nologin gallery

ARG OPENCV_ZOO_COMMIT=47534e27c9851bb1128ccc0102f1145e27f23f98
RUN mkdir -p /app/models \
    && python -c "import hashlib,pathlib,urllib.request; commit='${OPENCV_ZOO_COMMIT}'; files=[('face_detection_yunet/face_detection_yunet_2023mar.onnx','yunet.onnx','8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'),('face_recognition_sface/face_recognition_sface_2021dec.onnx','sface.onnx','0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79')]; base='https://github.com/opencv/opencv_zoo/raw/'+commit+'/models/'; [(lambda data,name,want: (hashlib.sha256(data).hexdigest()==want or (_ for _ in ()).throw(RuntimeError('bad checksum: '+name)), pathlib.Path('/app/models',name).write_bytes(data)))(urllib.request.urlopen(base+remote,timeout=120).read(),name,want) for remote,name,want in files]; pathlib.Path('/app/models/LICENSE').write_bytes(urllib.request.urlopen('https://raw.githubusercontent.com/opencv/opencv_zoo/'+commit+'/LICENSE',timeout=120).read())"

COPY --chown=gallery:gallery server.py ./server.py
COPY --chown=gallery:gallery smart_catalog.py ./smart_catalog.py
COPY --chown=gallery:gallery face_catalog.py ./face_catalog.py
COPY --chown=gallery:gallery static ./static

RUN mkdir -p /app/.cache /app/data /app/backups && chown gallery:gallery /app/.cache /app/data /app/backups

USER gallery

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3); c.request('GET','/health'); r=c.getresponse(); raise SystemExit(0 if r.status == 200 else 1)"]

CMD ["python", "server.py"]
