# One image, no host python: the bot and its tests both run from here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FL_DB=/app/data/fl.db \
    FL_HEALTH=/app/data/.heartbeat \
    FL_COOKIES=/app/secrets/cookies.txt

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY freelancer_manager ./freelancer_manager
COPY tests ./tests
# The suite asserts the deployment contract (one poller, mounts, healthcheck), so
# the deployment files are part of the test image too.
COPY docker-compose.yml ./docker-compose.yml
COPY Dockerfile ./Dockerfile

# State is mounted, never baked in: data/ (SQLite) and secrets/ (token, cookies).
# Both are created here so the image runs even when a volume is missing.
RUN mkdir -p /app/data /app/secrets \
    && useradd --system --uid 10001 --home /app bot \
    && chown -R bot:bot /app/data

USER bot

# The poll loop writes /app/data/.heartbeat on every pass; the probe reads it, so a
# wedged process is reported unhealthy instead of looking alive just because the
# container still exists.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-m", "freelancer_manager", "health"]

ENTRYPOINT ["python", "-m", "freelancer_manager"]
CMD ["run"]
