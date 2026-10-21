# syntax=docker/dockerfile:1
# Base pinned by multi-arch index digest (covers linux/amd64 + linux/arm64).
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# ---- builder: resolve and install dependencies into a self-contained venv ----
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY app/requirements.txt /tmp/requirements.txt
# --no-deps: install exactly the lock file; nothing unpinned can sneak in.
RUN /opt/venv/bin/pip install --no-deps -r /tmp/requirements.txt \
 && /opt/venv/bin/pip check   # incomplete lock -> the BUILD fails, not the running pod

# ---- runtime: base + venv + code, nothing else ----
FROM ${PYTHON_IMAGE}
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="anm-collector" \
      org.opencontainers.image.source="https://github.com/vbadita/anm-shortage-history" \
      org.opencontainers.image.revision="${GIT_SHA}"

# Fixed numeric UID/GID: Kubernetes runAsNonRoot can only verify a numeric user.
RUN groupadd --system --gid 10001 anm \
 && useradd --system --uid 10001 --gid anm --no-create-home --shell /usr/sbin/nologin anm \
 && mkdir /data && chown anm:anm /data

COPY --from=builder /opt/venv /opt/venv
COPY app/anm_collector /app/anm_collector
# Compile .pyc now: at runtime the root filesystem will be read-only.
RUN python -m compileall -q /app/anm_collector

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANM_DATA_DIR=/data \
    ANM_GIT_SHA=${GIT_SHA}

WORKDIR /app
USER 10001:10001
ENTRYPOINT ["python", "-m", "anm_collector"]
CMD ["sync"]
