ARG SIRIUS_ENV_CACHE_IMAGE=environment
ARG SIRIUS_BROWSER_CACHE_IMAGE=browser-empty
ARG SIRIUS_ENV_CACHE_KEY

# LLM 传输侧车（Go）。Python 侧已经没有任何传输实现，镜像里缺这个二进制应用就起不来，
# 所以它和 Python 依赖一样是构建期必需产物，而不是可选组件。构建参数与
# scripts/build_llm_transport.py 保持一致（-trimpath、./cmd/llm-transport）；关掉 CGO
# 是为了拿到静态链接的二进制，运行阶段的基础镜像与 glibc 版本因而无关紧要。
# 侧车只依赖标准库，这一步不需要网络（除拉取本阶段基础镜像）。
FROM golang:1.24.2-bookworm AS go-build

ENV CGO_ENABLED=0

WORKDIR /build

COPY go/llm-transport/ ./

RUN go build -trimpath -o /out/llm-transport ./cmd/llm-transport

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

# 缓存键必须盖在真正装系统包的这一层上。盖在 runtime 层时，标签取的是「本次构建
# 传入的键」，也就是当前 Dockerfile 的哈希——于是每次构建完标签都等于当前哈希，
# 判等永远成立，被复用的旧镜像再也不会被换掉（environment 阶段根本不会被构建）。
ARG SIRIUS_ENV_CACHE_KEY
LABEL org.sirius-pulse.environment-cache-key=$SIRIUS_ENV_CACHE_KEY

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps git gh \
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

WORKDIR /app

USER root
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
RUN rm -rf /app/sirius_pulse /app/sirius_pulse.egg-info \
    && mkdir -p /app/plugins \
    && chown sirius:sirius /app /app/plugins
COPY --chown=sirius:sirius sirius_pulse ./sirius_pulse
# 侧车二进制必须落在 sirius_pulse/utils/llm_sidecar.py 推导出的位置：
# PROJECT_ROOT（= /app）/ DEFAULT_BINARY_DIR（go/llm-transport/bin）/ BINARY_NAME，
# 而 Linux 下 BINARY_NAME 是 llm-transport，没有 .exe 后缀。换路径等于让镜像找不到侧车。
COPY --from=go-build --chown=sirius:sirius /out/llm-transport /app/go/llm-transport/bin/llm-transport
USER sirius
RUN uv sync --frozen --no-dev

# External plugins are supplied by the host checkout and mounted at runtime.
VOLUME ["/app/data", "/app/plugins"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"

ENTRYPOINT ["sirius-pulse"]
CMD ["webui", "--foreground"]
