# syntax=docker/dockerfile:1

# CUDA major version must match the one your llama.cpp was built with.
# Volta GPUs (V100, sm_70) need CUDA 12.x - CUDA 13 dropped them.
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04

# ---- frontend ---------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime ----------------------------------------------------------------
FROM ${CUDA_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp/libcurl are the usual runtime deps of a CUDA llama.cpp build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libgomp1 libcurl4 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/slotserve /app/backend/slotserve
COPY --from=web /src/backend/slotserve/static /app/static
RUN pip install /app/backend

ENV SLOTSERVE_HOST=0.0.0.0 \
    SLOTSERVE_PORT=8000 \
    SLOTSERVE_DATA_DIR=/data \
    SLOTSERVE_MODELS_DIR=/models \
    SLOTSERVE_STATIC_DIR=/app/static \
    SLOTSERVE_LLAMA_SERVER_CMD=/opt/llama.cpp/llama-server \
    LD_LIBRARY_PATH=/opt/llama.cpp:/usr/local/nvidia/lib:/usr/local/nvidia/lib64

VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["slotserve"]
