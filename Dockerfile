ARG SIRIUS_ENV_CACHE_IMAGE=environment
ARG SIRIUS_ENV_CACHE_KEY

FROM python:3.12-slim AS environment

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    SIRIUS_PULSE_HOME=/app \
    SIRIUS_PULSE_FILE_ROOT=/ \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && .venv/bin/python -m playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 sirius \
    && mkdir -p /app/data /ms-playwright \
    && chown -R sirius:sirius /app/.venv /app/data /ms-playwright

USER sirius

FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime

ARG SIRIUS_ENV_CACHE_KEY
LABEL org.sirius-pulse.environment-cache-key=$SIRIUS_ENV_CACHE_KEY

WORKDIR /app

COPY --chown=sirius:sirius sirius_pulse ./sirius_pulse
RUN uv sync --frozen --no-dev

VOLUME ["/app/data"]

EXPOSE 8080 18900

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"

ENTRYPOINT ["sirius-pulse"]
CMD ["webui", "--foreground"]
