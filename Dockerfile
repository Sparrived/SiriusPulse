FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    SIRIUS_PULSE_HOME=/app \
    SIRIUS_PULSE_FILE_ROOT=/ \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

COPY sirius_pulse ./sirius_pulse
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 sirius \
    && mkdir -p /app/data \
    && chown -R sirius:sirius /app/.venv /app/data

USER sirius

VOLUME ["/app/data"]

EXPOSE 8080 18900

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"

ENTRYPOINT ["sirius-pulse"]
CMD ["webui", "--foreground"]
