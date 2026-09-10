FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY src ./src

RUN useradd --create-home appuser \
    && mkdir --parents /app/data_cache \
    && chown --recursive appuser:appuser /app/data_cache

# Mounted volumes retain ownership from previous deployments. Reclaim only
# the cache directories, then drop privileges before serving any requests.
CMD ["python", "src/container_runtime.py"]
