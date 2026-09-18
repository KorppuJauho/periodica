# syntax=docker/dockerfile:1
# Base image pinned by digest; Dependabot keeps it updated.
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

# --- dependencies ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
# Hash-checked, wheels only: no source builds, no compilers.
RUN /venv/bin/pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.txt

FROM builder AS release-deps
RUN /venv/bin/python -m pip uninstall -y pip

FROM builder AS test-deps
COPY requirements-test.txt /tmp/requirements-test.txt
RUN /venv/bin/pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements-test.txt \
    && /venv/bin/python -m pip uninstall -y pip

# --- runtime base ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    # pip is not needed at runtime; removing it shrinks the attack surface.
    && python -m pip uninstall -y pip \
    && useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin periodica \
    && mkdir -p /config /data \
    && chown 10001:10001 /config
COPY periodica /app/periodica
ARG GIT_COMMIT=""
ARG APP_VERSION=""
ENV PATH=/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/data \
    CONFIG_DIR=/config \
    PORT=8765 \
    PERIODICA_COMMIT=${GIT_COMMIT} \
    PERIODICA_VERSION=${APP_VERSION}
USER 10001
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8765'), timeout=4)"]
ENTRYPOINT ["python", "-m", "periodica"]

# --- tests inside the real runtime image (poppler, non-root, Linux) -------------------------------
FROM base AS test
COPY --from=test-deps /venv /venv
COPY pyproject.toml /app/pyproject.toml
COPY tests /app/tests
WORKDIR /app
ENV HOME=/tmp
RUN python -m pytest -q -p no:cacheprovider

# --- lint and type checks (scripts/test-local.sh lint; CI runs the same tools directly) -------------
FROM builder AS lint
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN /venv/bin/pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements-dev.txt
WORKDIR /src
COPY pyproject.toml ./
COPY periodica periodica
COPY tests tests
COPY scripts scripts
RUN /venv/bin/ruff check . && /venv/bin/mypy periodica && /venv/bin/bandit -q -c pyproject.toml -r periodica

# --- integration tests against a real qBittorrent (tests/integration/compose.yml) -------------------
FROM base AS integration
COPY --from=test-deps /venv /venv
COPY pyproject.toml /app/pyproject.toml
COPY tests /app/tests
WORKDIR /app
ENV HOME=/tmp
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "tests/integration"]

# --- default target -------------------------------------------------------------------------------
FROM base AS release
COPY --from=release-deps /venv /venv
