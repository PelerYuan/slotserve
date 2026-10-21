# Adds this Windows PC to a SlotServe server as a node: the agent runs in Docker Desktop (WSL2 backend), with the
# NVIDIA GPU when there is one. The SlotServe server hands this script out with the server address and the node
# token filled in. In PowerShell:
#
#   irm "https://slotserve.example/install/node.ps1?token=ssn-..." | iex
#
# It checks Docker Desktop, its WSL2 engine and GPU support, then starts the node agent container with a restart
# policy. Running it again replaces the container (e.g. after a new token). Optional settings, as environment
# variables set before running it ($env:NAME = 'value'):
#
#   SLOTSERVE_HOME            base folder for the two below (default %USERPROFILE%\slotserve)
#   SLOTSERVE_MODELS_DIR      this PC's models folder (default $SLOTSERVE_HOME\models)
#   SLOTSERVE_LLAMA_CPP_DIR   folder with a *Linux* build of llama.cpp (default $SLOTSERVE_HOME\llama.cpp)
#   SLOTSERVE_IMAGE           the SlotServe image (default: the one the server suggests)
#   SLOTSERVE_NO_GPU=1        join as a CPU-only node
#   SLOTSERVE_AGENT_WOL_MAC   MAC address(es) for Wake-on-LAN (default: the active wired/wireless adapter)
#   SLOTSERVE_AGENT_NETWORK   label of this local network (default: the adapter's subnet, e.g. 192.168.1.0/24)

& {
  $ErrorActionPreference = 'Stop'

  function Say([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
  function Warn([string]$m) { Write-Host "warning: $m" -ForegroundColor Yellow }
  function Fail([string]$m) { Write-Host "error: $m" -ForegroundColor Red; throw "SlotServe node setup stopped: $m" }
  function Pick([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v }
  }

  $Server = Pick 'SLOTSERVE_AGENT_SERVER' '__SLOTSERVE_SERVER__'
  $Token = Pick 'SLOTSERVE_AGENT_TOKEN' '__SLOTSERVE_TOKEN__'
  $Image = Pick 'SLOTSERVE_IMAGE' '__SLOTSERVE_IMAGE__'
  $Base = Pick 'SLOTSERVE_HOME' (Join-Path $env:USERPROFILE 'slotserve')
  $ModelsDir = Pick 'SLOTSERVE_MODELS_DIR' (Join-Path $Base 'models')
  $LlamaDir = Pick 'SLOTSERVE_LLAMA_CPP_DIR' (Join-Path $Base 'llama.cpp')
  $WolMac = Pick 'SLOTSERVE_AGENT_WOL_MAC' ''
  $Network = Pick 'SLOTSERVE_AGENT_NETWORK' ''
  $Name = 'slotserve-agent'

  if (-not $Token) { Fail 'no node token. Use the command from Admin > Nodes, or set $env:SLOTSERVE_AGENT_TOKEN.' }
  if (-not $Server) { Fail 'no server address. Set $env:SLOTSERVE_AGENT_SERVER, e.g. http://slotserve.lan:8000.' }

  # ---- Docker Desktop and WSL2 -------------------------------------------------------------------------------
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'Docker Desktop is not installed: https://docs.docker.com/desktop/setup/install/windows-install/'
  }
  $osType = (& docker info --format '{{.OSType}}' 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $osType) { Fail 'Docker Desktop is not running. Start it and run this again.' }
  if ($osType.Trim() -ne 'linux') { Fail 'Docker Desktop runs Windows containers. Switch to Linux containers (tray icon > Switch to Linux containers).' }
  $kernel = (& docker info --format '{{.KernelVersion}}' 2>$null)
  if ($kernel -notmatch 'microsoft|WSL') {
    Warn "Docker Desktop does not use the WSL 2 engine (kernel: $kernel). GPU support needs it: Settings > General > Use the WSL 2 based engine."
  } else {
    Say "Docker Desktop with the WSL 2 engine ($($kernel.Trim()))"
  }

  # ---- GPU ----------------------------------------------------------------------------------------------------
  $gpuArgs = @()
  $hasNvidia = $false
  if ((Pick 'SLOTSERVE_NO_GPU' '') -eq '1') {
    Say 'SLOTSERVE_NO_GPU=1: joining as a CPU-only node'
  } elseif (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
    $gpus = (& nvidia-smi.exe -L 2>$null)
    if ($LASTEXITCODE -eq 0 -and $gpus) { $hasNvidia = $true; Say "NVIDIA GPU(s): $(@($gpus).Count)" }
  }
  if (-not $hasNvidia -and (Pick 'SLOTSERVE_NO_GPU' '') -ne '1') { Warn 'no NVIDIA GPU or driver found: joining as a CPU-only node' }

  Say "pulling $Image (the first time this takes a while)"
  & docker pull $Image | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "could not pull $Image" }

  if ($hasNvidia) {
    & docker run --rm --gpus all --entrypoint nvidia-smi $Image -L 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
      $gpuArgs = @('--gpus', 'all')
      Say 'containers can use the GPU'
    } else {
      Fail 'containers cannot use the GPU. Update the NVIDIA driver and Docker Desktop, and use the WSL 2 engine (or set $env:SLOTSERVE_NO_GPU = 1).'
    }
  }

  # ---- Wake-on-LAN: the real network card (the container only sees WSL's virtual one) --------------------------
  $adapter = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' |
    Sort-Object @{ Expression = { $_.MediaType -ne '802.3' } }, ifIndex | Select-Object -First 1
  if ($adapter) {
    if (-not $WolMac) { $WolMac = $adapter.MacAddress -replace '-', ':' }
    $ip = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
    if ($ip -and -not $Network) {
      $bytes = ([System.Net.IPAddress]::Parse($ip.IPAddress)).GetAddressBytes()
      [Array]::Reverse($bytes)
      $addr = [BitConverter]::ToUInt32($bytes, 0)
      $mask = if ($ip.PrefixLength -eq 0) { [uint32]0 } else { [uint32](([uint64]4294967295 -shl (32 - $ip.PrefixLength)) -band [uint64]4294967295) }
      $netBytes = [BitConverter]::GetBytes([uint32]($addr -band $mask))
      [Array]::Reverse($netBytes)
      $Network = "$(([System.Net.IPAddress]::new($netBytes)).ToString())/$($ip.PrefixLength)"
    }
    $magic = Get-NetAdapterAdvancedProperty -Name $adapter.Name -RegistryKeyword '*WakeOnMagicPacket' -ErrorAction SilentlyContinue
    if ($magic -and "$($magic.RegistryValue)" -ne '1') {
      Warn "Wake on Magic Packet is off for $($adapter.Name) (Device Manager > adapter > Advanced). Needed to wake this PC remotely."
    }
    if ($adapter.MediaType -ne '802.3') { Warn "$($adapter.Name) is not wired; most PCs can only be woken over Ethernet." }
    Say "network card for Wake-on-LAN: $($adapter.Name) $WolMac ($Network)"
  } else {
    Warn 'no active physical network card found; Wake-on-LAN details can be entered in SlotServe later.'
  }

  # ---- folders ------------------------------------------------------------------------------------------------
  New-Item -ItemType Directory -Force -Path $ModelsDir, $LlamaDir | Out-Null
  if (-not (Test-Path (Join-Path $LlamaDir 'llama-server'))) {
    Warn "no llama-server in $LlamaDir. The agent runs in a Linux container: put a *Linux* build of llama.cpp there"
    Warn '(the CUDA build for NVIDIA GPUs), not the Windows .exe. The node still joins, but cannot run models until then.'
  }

  # ---- the agent ----------------------------------------------------------------------------------------------
  & docker container inspect $Name 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Say "replacing the existing $Name container"
    & docker rm -f $Name | Out-Null
  }
  $runArgs = @('run', '-d', '--name', $Name, '--restart', 'unless-stopped') + $gpuArgs + @(
    '-v', "${LlamaDir}:/opt/llama.cpp:ro",
    '-v', "${ModelsDir}:/models",
    '-v', 'slotserve-agent:/data',
    '-v', '/var/run/docker.sock:/var/run/docker.sock',
    '-e', "SLOTSERVE_AGENT_SERVER=$Server",
    '-e', 'SLOTSERVE_AGENT_MODEL_HOST=127.0.0.1',
    '-e', "SLOTSERVE_AGENT_TOKEN=$Token",
    '-e', 'SLOTSERVE_AGENT_HOST_OS=windows',
    '-e', "SLOTSERVE_AGENT_WOL_MAC=$WolMac",
    '-e', "SLOTSERVE_AGENT_NETWORK=$Network",
    $Image, 'slotserve', 'agent'
  )
  Say 'starting the SlotServe node agent'
  & docker @runArgs | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'docker run failed' }
  Start-Sleep -Seconds 3
  if ((& docker inspect -f '{{.State.Running}}' $Name) -ne 'true') {
    & docker logs --tail 30 $Name
    Fail 'the agent stopped right away (see the log above)'
  }
  Say 'done. The PC shows up as Online in SlotServe within seconds.'
  Say "Keep Docker Desktop starting with Windows (Settings > General > Start Docker Desktop when you sign in)."
  Say "models folder: $ModelsDir    logs: docker logs -f $Name"
}
