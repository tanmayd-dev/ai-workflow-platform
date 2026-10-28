# syntax=docker/dockerfile:1

# python:3.12-slim multi-arch index digest.
# Verified 2026-10-27 against the Docker Hub registry API: the index contains
# linux/amd64 and linux/arm64 manifests. Re-resolve with:
#   docker buildx imagetools inspect python:3.12-slim
# (We cannot build images in this environment; the digest was checked online.)
FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f

WORKDIR /app

# Runtime configuration comes from the environment (see .env.example).
# No credentials or data paths are baked into the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    AUTO_CREATE_SCHEMA=false \
    APP_UID=10001 \
    APP_GID=10001 \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache

# Unprivileged runtime user; the entrypoint chowns the data volumes to it.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic alembic/
COPY app app/
COPY scripts scripts/
COPY docker/entrypoint.py /usr/local/bin/entrypoint.py
RUN chmod 0755 /usr/local/bin/entrypoint.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

# The entrypoint prepares volumes, runs `alembic upgrade head`, drops to
# APP_UID/APP_GID, then execs this command.
ENTRYPOINT ["python", "/usr/local/bin/entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
