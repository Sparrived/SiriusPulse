ARG SIRIUS_ENV_CACHE_IMAGE=environment
ARG SIRIUS_BROWSER_CACHE_IMAGE=browser-empty
ARG SIRIUS_ENV_CACHE_KEY

FROM python:3.12-slim AS browser-empty

RUN mkdir -p /ms-playwright

FROM ${SIRIUS_BROWSER_CACHE_IMAGE} AS browser-cache

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
    && uv sync --frozen --no-dev --no-install-project

# Chromium 从上一版镜像里复制过来（见 scripts/update-container.sh 的
# SIRIUS_BROWSER_CACHE_IMAGE），playwright install 发现版本已存在就不再下载。
# 直接从 CDN 拉很慢且偶尔超时，这一步是让日常重建不必重下浏览器的关键。
COPY --from=browser-cache /ms-playwright/ /ms-playwright/
RUN .venv/bin/python -m playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 sirius \
    && mkdir -p /app/data /ms-playwright \
    && chown -R sirius:sirius /app/.venv /app/data /ms-playwright

USER sirius

FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime

ARG SIRIUS_ENV_CACHE_KEY
LABEL org.sirius-pulse.environment-cache-key=$SIRIUS_ENV_CACHE_KEY

WORKDIR /app

USER root
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
RUN rm -rf /app/sirius_pulse /app/sirius_pulse.egg-info \
    && mkdir -p /app/plugins \
    && chown sirius:sirius /app /app/plugins
COPY --chown=sirius:sirius sirius_pulse ./sirius_pulse
USER sirius
RUN uv sync --frozen --no-dev

# External plugins are supplied by the host checkout and mounted at runtime.
VOLUME ["/app/data", "/app/plugins"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"

ENTRYPOINT ["sirius-pulse"]
CMD ["webui", "--foreground"]
