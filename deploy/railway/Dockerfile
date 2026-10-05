# Prism always-on authoritative runtime (Railway). Built only from `git archive <commit>` by
# deploy/railway/deploy.sh, never from a working tree; REVISION records that exact commit.
# Python and every package are pinned (base image + uv.lock), matching the development host.
FROM python:3.12.3-slim-bookworm

ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA256=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1
ARG UV_VERSION=0.12.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
 && chmod +x /usr/local/bin/supercronic \
 && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN test -s REVISION && uv sync --frozen --no-dev \
 && chmod +x deploy/railway/entrypoint.sh

# Not secret and not authority: the role/runtime id are Railway service variables, so this
# image run anywhere else is non-authoritative and refuses production jobs.
ENV PATH=/app/.venv/bin:$PATH PRISM_HOME=/app PRISM_DB_PATH=/data/prism.duckdb \
    PRISM_RAW_DIR=/data/raw PRISM_BACKUP_DIR=/data/backups PRISM_LOG_DIR=/data/logs \
    PYTHONUNBUFFERED=1 TZ=UTC

ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/railway/entrypoint.sh"]
