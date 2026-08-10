# One image, two entry points: the API server and the fetcher (see docker-compose.yml).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: cached until the lockfile or a package manifest changes.
COPY pyproject.toml uv.lock ./
COPY packages/eifo-core/pyproject.toml packages/eifo-core/
COPY packages/eifo-api/pyproject.toml packages/eifo-api/
COPY packages/eifo-fetcher/pyproject.toml packages/eifo-fetcher/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-workspace --no-dev --no-editable

COPY packages/ packages/
# --no-editable copies the packages into the venv, so the runtime stage needs
# only /app/.venv and not the source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

RUN useradd --create-home --uid 1000 eifo
WORKDIR /app

COPY --from=builder --chown=eifo:eifo /app/.venv /app/.venv
COPY --chown=eifo:eifo web/ web/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EIFO_DB_URL="sqlite:///data/eifo.db" \
    EIFO_IMAGES_DIR="data/images"

USER eifo
EXPOSE 8000

# Overridden by the fetcher service in docker-compose.yml.
CMD ["uvicorn", "eifo_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
