# syntax=docker/dockerfile:1.7

# ---- build: resolve and install dependencies with uv -------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so they cache independently of the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself.
COPY README.md ./
COPY src/ ./src/
COPY workspace/ ./workspace/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim image, non-root, no build tooling -------------------------
FROM python:3.13-slim-bookworm AS runtime

# The app writes into the mounted workspace, so it has to run as the owner of
# those files. Override at build time to match the host: --build-arg UID=$(id -u).
ARG UID=1000
ARG GID=1000

# git: the app records the workspace commit when the workspace is a repository.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git; \
    rm -rf /var/lib/apt/lists/*; \
    getent group "$GID" >/dev/null || groupadd --gid "$GID" wf; \
    useradd --uid "$UID" --gid "$GID" --home-dir /app --shell /usr/sbin/nologin wf

WORKDIR /app

COPY --from=build --chown=wf:wf /app/.venv ./.venv
COPY --from=build --chown=wf:wf /app/src ./src
COPY --from=build --chown=wf:wf /app/workspace ./workspace
COPY --from=build --chown=wf:wf /app/README.md /app/pyproject.toml ./

# artifacts and the default SQLite database live here; mount a volume in compose
RUN mkdir -p /app/var && chown -R wf:wf /app/var

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WF_WORKSPACE=/app/workspace

USER wf

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["wf", "serve", "--host", "0.0.0.0", "--port", "8000"]
