# syntax=docker/dockerfile:1

# CUDA major version must match the one your llama.cpp was built with.
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04

# ---- frontend ---------------------------------------------------------------
FROM node:26-alpine AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- SSH server for dev environments ------------------------------------------
# A static (musl) Dropbear, copied into dev environment containers so any image can be reached over SSH.
FROM alpine:3.22 AS dropbear
ARG DROPBEAR_VERSION=2026.94
ARG DROPBEAR_SHA256=e098034a843699200c8c977a991fff73159735bf795d5f72ef672c41a6b1ae81
RUN apk add --no-cache build-base curl
RUN curl -fsSL -o /tmp/dropbear.tar.bz2 "https://matt.ucc.asn.au/dropbear/releases/dropbear-${DROPBEAR_VERSION}.tar.bz2" \
 && echo "${DROPBEAR_SHA256}  /tmp/dropbear.tar.bz2" | sha256sum -c - \
 && tar xjf /tmp/dropbear.tar.bz2 -C /tmp \
 && cd "/tmp/dropbear-${DROPBEAR_VERSION}" \
 && ./configure --enable-static --disable-zlib --disable-syslog --disable-lastlog --disable-wtmp --disable-wtmpx \
      --disable-utmp --disable-utmpx --disable-pututline --disable-pututxline \
 && make -j"$(nproc)" PROGRAMS="dropbear dropbearkey" MULTI=1 STATIC=1 \
 && strip dropbearmulti \
 && install -D -m 755 dropbearmulti /out/dropbearmulti

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
COPY --from=dropbear /out/dropbearmulti /opt/slotserve/bin/dropbearmulti
RUN pip install /app/backend

ENV SLOTSERVE_HOST=0.0.0.0 \
    SLOTSERVE_PORT=8000 \
    SLOTSERVE_DATA_DIR=/data \
    SLOTSERVE_MODELS_DIR=/models \
    SLOTSERVE_STATIC_DIR=/app/static \
    SLOTSERVE_LLAMA_SERVER_CMD=/opt/llama.cpp/llama-server \
    LD_LIBRARY_PATH=/opt/llama.cpp:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/lib/wsl/lib

VOLUME ["/data", "/models"]
EXPOSE 8000 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["slotserve"]
