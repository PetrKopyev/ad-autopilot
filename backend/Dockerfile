# Базовый образ закреплён по digest: обновлять осознанно, вместе с проверкой сборки.
FROM python:3.12-slim@sha256:5b8b1421ccb6a525860a77863c049eab5847076cda34e030c75a80c58faf3f3c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MEDIA_DIR=/data/media

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Зависимости ставятся только из lock-файла с хешами; само приложение — исходники в /app (PYTHONPATH).
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/media \
    && chown 10001:10001 /data/media

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
