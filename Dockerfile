FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY freelancer_manager ./freelancer_manager
COPY tests ./tests

# State is mounted, not baked in.
RUN mkdir -p /app/data /app/secrets

ENTRYPOINT ["python", "-m", "freelancer_manager"]
CMD ["run"]
