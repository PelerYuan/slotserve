#!/usr/bin/env bash
# Adds this machine to a SlotServe server as a node (Linux with or without NVIDIA GPUs; macOS as a CPU-only node).
# The SlotServe server hands this script out with the server address and the node token filled in:
#
#   curl -fsSL "https://slotserve.example/install/node.sh?token=ssn-..." | bash
#
# It checks Docker (and the NVIDIA Container Toolkit when the machine has an NVIDIA GPU) and starts the node agent
# container with a restart policy, so the machine rejoins by itself after a reboot. Running it again replaces the
# container (e.g. after a new token). Optional settings, as environment variables:
#
#   SLOTSERVE_HOME            base folder for the two below (default ~/slotserve)
#   SLOTSERVE_MODELS_DIR      this machine's models folder (default $SLOTSERVE_HOME/models)
#   SLOTSERVE_LLAMA_CPP_DIR   folder with a Linux build of llama.cpp (llama-server) (default $SLOTSERVE_HOME/llama.cpp)
#   SLOTSERVE_IMAGE           the SlotServe image (default: the one the server suggests)
#   SLOTSERVE_NO_GPU=1        join as a CPU-only node even with an NVIDIA GPU
#   SLOTSERVE_AGENT_WOL_MAC   MAC address(es) for Wake-on-LAN (default: detected)
#   SLOTSERVE_AGENT_NETWORK   label of this local network (machines with the same label wake each other)
set -euo pipefail

SERVER="${SLOTSERVE_AGENT_SERVER:-__SLOTSERVE_SERVER__}"
TOKEN="${SLOTSERVE_AGENT_TOKEN:-__SLOTSERVE_TOKEN__}"
IMAGE="${SLOTSERVE_IMAGE:-__SLOTSERVE_IMAGE__}"
BASE="${SLOTSERVE_HOME:-$HOME/slotserve}"
MODELS_DIR="${SLOTSERVE_MODELS_DIR:-$BASE/models}"
LLAMA_DIR="${SLOTSERVE_LLAMA_CPP_DIR:-$BASE/llama.cpp}"
WOL_MAC="${SLOTSERVE_AGENT_WOL_MAC:-}"
NETWORK="${SLOTSERVE_AGENT_NETWORK:-}"
NAME=slotserve-agent

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

[ -n "$TOKEN" ] || die "no node token. Use the command from Admin > Nodes, or set SLOTSERVE_AGENT_TOKEN."
[ -n "$SERVER" ] || die "no server address. Set SLOTSERVE_AGENT_SERVER, e.g. http://slotserve.lan:8000."

# ---- Docker ----------------------------------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker is not installed: https://docs.docker.com/engine/install/ (macOS: Docker Desktop)"
docker info >/dev/null 2>&1 || die "cannot talk to Docker. Is it running? Is $(id -un) in the docker group? (or run with sudo)"

OS="$(uname -s)"
HOST_OS=linux
GPU_ARGS=()
NET_ARGS=()
if [ "$OS" = "Darwin" ]; then
  HOST_OS=macos
  warn "macOS: containers cannot use the Mac's GPU, so this machine joins as a CPU-only node."
elif [ "${SLOTSERVE_NO_GPU:-}" = "1" ]; then
  say "SLOTSERVE_NO_GPU=1: joining as a CPU-only node"
elif command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  say "NVIDIA GPU(s) found: $(nvidia-smi -L | wc -l | tr -d ' ')"
  if docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia' || command -v nvidia-ctk >/dev/null 2>&1 \
    || command -v nvidia-container-toolkit >/dev/null 2>&1; then
    GPU_ARGS=(--gpus all)
  else
    die "the NVIDIA Container Toolkit is missing: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html (or set SLOTSERVE_NO_GPU=1 for a CPU-only node)"
  fi
else
  warn "no NVIDIA GPU or driver found: joining as a CPU-only node"
fi

if [ "$OS" = "Linux" ]; then
  # The agent connects out to the server, so no port has to be opened. Host networking lets it see the real
  # network card (for Wake-on-LAN) and send magic packets to its neighbours.
  NET_ARGS=(--network host)
elif [ -z "$WOL_MAC" ] && command -v route >/dev/null 2>&1; then
  iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
  [ -n "$iface" ] && WOL_MAC="$(ifconfig "$iface" 2>/dev/null | awk '/ether /{print $2; exit}')"
fi

# ---- folders ---------------------------------------------------------------------------------------------------
mkdir -p "$MODELS_DIR" "$LLAMA_DIR"
if [ ! -x "$LLAMA_DIR/llama-server" ]; then
  warn "no llama-server in $LLAMA_DIR. Put a Linux build of llama.cpp there (CUDA build for NVIDIA GPUs);"
  warn "the node still joins, but cannot run models until it is there."
fi

# ---- the agent -------------------------------------------------------------------------------------------------
say "pulling $IMAGE"
docker pull "$IMAGE" >/dev/null
if docker container inspect "$NAME" >/dev/null 2>&1; then
  say "replacing the existing $NAME container"
  docker rm -f "$NAME" >/dev/null
fi
say "starting the SlotServe node agent"
docker run -d --name "$NAME" --restart unless-stopped \
  ${GPU_ARGS[@]+"${GPU_ARGS[@]}"} ${NET_ARGS[@]+"${NET_ARGS[@]}"} \
  -v "$LLAMA_DIR:/opt/llama.cpp:ro" \
  -v "$MODELS_DIR:/models" \
  -v slotserve-agent:/data \
  -e SLOTSERVE_AGENT_SERVER="$SERVER" \
  -e SLOTSERVE_AGENT_TOKEN="$TOKEN" \
  -e SLOTSERVE_AGENT_HOST_OS="$HOST_OS" \
  -e SLOTSERVE_AGENT_WOL_MAC="$WOL_MAC" \
  -e SLOTSERVE_AGENT_NETWORK="$NETWORK" \
  "$IMAGE" slotserve agent >/dev/null

sleep 3
if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
  docker logs --tail 30 "$NAME" >&2 || true
  die "the agent stopped right away (see the log above)"
fi
say "done. The machine shows up as Online in SlotServe within seconds."
say "models folder: $MODELS_DIR    logs: docker logs -f $NAME"
