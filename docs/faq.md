# FAQ

**A model takes minutes to load. Can SlotServe make it faster?**
Loading is bound by disk speed. Put frequently used models on an SSD (point `MODELS_DIR` at it, or symlink individual files). Linux caches recently read files in free RAM, so on a host with more RAM than the model size a second load is much faster. SlotServe hides the wait by preloading before bookings.

**My client gets `503 model_unavailable`.**
The model is loading or failed to start. The `Retry-After` header says roughly how long to wait. If it keeps failing, open **Models → Server log** to see the model server's error (often an out-of-memory error from a too large `-c` or a bad argument).

**My client gets `409 model_not_loaded`.**
The request named another registered model. Either omit `model`/use the loaded one, or book a slot with the model you want.

**Can several people share the GPU at the same time?**
Yes, when nobody has booked it: every valid key can use the default model concurrently (raise `-np` in its launch arguments for more parallel slots). During a booking the GPU is exclusive.

**Does it support multiple GPUs or multiple models at once?**
Yes. In **Admin → GPUs & lanes** split the GPUs into lanes; each lane runs its own model and is booked separately, so several models are loaded at once. To use several GPUs for one model, put them in one lane and pass the engine's multi-GPU flags (for llama.cpp `--split-mode`/`--tensor-split`, for vLLM `--tensor-parallel-size`). See [GPUs, lanes & engines](gpus-and-engines.md).

**Can I use vLLM or SGLang instead of llama.cpp?**
Yes, per model: **Models → Add model** and pick the engine. The engine must be installed where SlotServe runs; `Dockerfile.vllm` builds an image with vLLM.

**I forgot my password.**
Another admin resets it in **Admin → Users**, or on the server: `docker compose exec slotserve slotserve reset-password <username>`. See [Forgotten admin password or locked out](operations.md#admin-password).

**How do I put it behind HTTPS / a reverse proxy?**
Proxy port 8000 with the original `Host` header, WebSockets and buffering off, set `SLOTSERVE_PUBLIC_URL`, `SLOTSERVE_COOKIE_SECURE=true` and `SLOTSERVE_FORWARDED_ALLOW_IPS`, and give the dev environment web port (8001) an address of its own (`SLOTSERVE_DEVENV_WEB_URL`). The full nginx example is in [Reverse proxy](operations.md#reverse-proxy).

**Changes in the web UI fail with "did not come from a SlotServe page".**
A reverse proxy rewrites the `Host` header. Pass the original one (`proxy_set_header Host $host;`) or set `SLOTSERVE_PUBLIC_URL` to the address people use; see [Reverse proxy](operations.md#reverse-proxy).

**How do I back up?**
SlotServe writes a backup bundle every day (database, generated secret key, SSH host keys) to `DATA_DIR/backups/`. Set **Copy backups to** so a copy lands on another machine, and back up workspaces and models where they live. See [Backups](operations.md#backups) and [Restore from a backup](operations.md#restore).

**`ssh` says REMOTE HOST IDENTIFICATION HAS CHANGED.**
Once per environment after the upgrade to v0.10 this is expected: every environment now has its own host key. Compare the fingerprint with the **Host key** on the environment's card, then `ssh-keygen -R "[server]:port"`. See [Upgrade from v0.8](operations.md#upgrade-after).

**`sudo` does not work in my dev environment.**
Environments run with "no new privileges"; you are `root` already, so leave `sudo` out. Images that need it can run in compatibility mode; see [Container hardening](operations.md#hardening).

**How do I see why a model does not load?**
Open **Admin → Runtime monitor**. It shows the loading stage and progress, the live output of the model server, and for a failure the error, exit code, the last lines of output, the exact command that was run and, for errors SlotServe recognises, a diagnosis (below). Every earlier load is listed with the same details.

**Where do the model server logs go?**
`DATA_DIR/logs/llama-server.log` for the first lane and `llama-server-lane<N>.log` for the others (rotated at 20 MB), also visible in the UI. The file name is the same for every engine.

## Diagnoses {#diagnosis}

When a model load, a dev environment, a storage or a health check fails, SlotServe compares the error and the last log lines with known failures. A recognised one comes with a **diagnosis** - the likely cause, the next steps and the log lines that matched - on the runtime monitor, the load history and the health table (Admin → Overview). The same causes and steps are listed here, so you can also search this page for an error text.

An unrecognised failure keeps its exit code and log tail: the error is usually near the end. Use **Copy diagnosis** on the card when you ask for help.

### The port is already in use by another program {#port-in-use}

Log lines like: `couldn't bind HTTP server socket`, `address already in use`, `port is already allocated`

- Find and stop the program that holds the port (often a llama-server or vLLM started by hand, or an old service). Linux: ss -ltnp; Windows: netstat -ano
- Or move SlotServe's ports away from it: SLOTSERVE_LLAMA_PORT for the model servers (each lane uses the next port), the SSH and direct port ranges for dev environments; then restart SlotServe
- Retry the load once the port is free

### The disk is full {#disk-full}

Log lines like: `No space left on device`, `[Errno 28]`

- Free space on the disk named in the log: the models folder, the data directory or Docker's data root (docker system df; docker image prune)
- Remove models or saved environment images that are no longer needed
- Retry once there is enough room

### The context length does not fit into the GPU memory left for the KV cache {#context-too-long}

Log lines like: `max seq len (...) is larger than the maximum number of tokens that can be stored in KV cache`

- Lower --max-model-len
- Or raise --gpu-memory-utilization (up to about 0.95) when nothing else uses the GPU
- Or use more GPUs (--tensor-parallel-size) or a smaller / more quantized model

### The GPU ran out of memory while loading the model {#gpu-out-of-memory}

Log lines like: `cudaMalloc failed: out of memory`, `CUDA out of memory`, `torch.OutOfMemoryError`

- Lower the context size (llama.cpp -c, vLLM --max-model-len): the KV cache grows with it
- Offload fewer layers to the GPU (llama.cpp -ngl) or use a smaller quantization of the model
- Check that nothing else uses the GPU (nvidia-smi): another model, a dev environment or a job

### The process was stopped because the machine ran out of RAM {#host-out-of-memory}

Log lines like: `Killed`, `Out of memory: Killed process`, `std::bad_alloc`

- Check free memory (free -h) and what else is running; dev environments and other models use RAM too
- Use a smaller model or quantization; with llama.cpp put more layers on the GPU (-ngl) and do not use --no-mmap
- For dev environments: set a memory limit so one container cannot take everything

### Docker cannot give the container a GPU: the NVIDIA Container Toolkit is missing or not set up {#gpu-runtime-missing}

Log lines like: `could not select device driver "nvidia" with capabilities: [[gpu]]`, `nvidia-container-cli: initialization error`

- Install the NVIDIA Container Toolkit on the Docker host, run sudo nvidia-ctk runtime configure --runtime=docker and restart Docker
- Check that the host driver works (nvidia-smi) and test: docker run --rm --gpus all ubuntu nvidia-smi
- Docker Desktop on Windows: use the WSL 2 backend with a current NVIDIA driver

### The engine's CUDA version does not match the GPU driver or the GPU {#cuda-mismatch}

Log lines like: `CUDA driver version is insufficient for CUDA runtime version`, `no kernel image is available for execution on the device`

- Update the NVIDIA driver on the host, or use an engine build for an older CUDA version
- With Docker: the CUDA major version of CUDA_IMAGE must match the one llama-server was built with
- A very new or very old GPU needs a build that includes its architecture (CMAKE_CUDA_ARCHITECTURES)

### The engine cannot start: a library it needs (a DLL or .so) is missing {#engine-library-missing}

Log lines like: `error while loading shared libraries: libcudart.so.12: cannot open shared object file`

- Usually the CUDA runtime does not match the engine build: install the matching CUDA runtime or use an engine build for the installed CUDA
- Windows: put the cudart DLLs from the llama.cpp release next to llama-server.exe
- Linux: ldd $(which llama-server) shows which libraries are missing

### The engine program cannot be run (not executable or built for another system) {#engine-not-executable}

Log lines like: `spawn failed: [Errno 13] Permission denied`, `exec format error`

- Make it executable: chmod +x <path to llama-server>
- Check that the build matches this machine (x86_64 or arm64, Linux or Windows)
- A folder mounted with noexec cannot run programs: move the engine or remount without noexec

### The engine program was not found {#engine-not-found}

Log lines like: `spawn failed: [Errno 2] No such file or directory`, `command not found`

- Set the full path of the engine: SLOTSERVE_LLAMA_SERVER_CMD for llama.cpp, SLOTSERVE_VLLM_CMD for vLLM; then restart SlotServe
- With Docker: use the image variant that contains the engine (the vLLM image for vLLM models)
- Check that the model uses the engine you installed (Models → engine)

### SlotServe cannot reach Docker {#docker-unreachable}

Log lines like: `Cannot connect to the Docker daemon at unix:///var/run/docker.sock`, `permission denied while trying to connect to the Docker daemon socket`

- Check that Docker is running on the host (docker info)
- In Docker: mount the socket into SlotServe (-v /var/run/docker.sock:/var/run/docker.sock); outside Docker: the user running SlotServe needs access to it (docker group)
- Check SLOTSERVE_DOCKER_HOST if Docker listens elsewhere (e.g. Podman's socket or tcp://)

### The Docker image could not be downloaded {#image-pull-failed}

Log lines like: `pull access denied for ...`, `manifest unknown`, `toomanyrequests`

- Check the image name and tag (typos, a tag that does not exist)
- Check that the Docker host can reach the registry (proxy, firewall, a registry mirror in /etc/docker/daemon.json)
- Private images or Docker Hub's rate limit: docker login on the host, or pull the image once by hand (docker pull)

### The Docker host cannot mount this NFS/SMB share: the mount helper is missing {#mount-helper-missing}

Log lines like: `you might need a /sbin/mount.<type> helper program`, `wrong fs type, bad option, bad superblock`

- Install the helper on the Docker host (not in the container): nfs-common (Debian/Ubuntu) or nfs-utils (RHEL) for NFS, cifs-utils for SMB
- Then click Check on the storage again
- Docker Desktop: the mount happens in its VM; use a host directory storage instead

### The storage server refused the mount (wrong credentials or not allowed) {#mount-access-denied}

Log lines like: `mount.nfs: access denied by server while mounting ...`, `mount error(13): Permission denied`

- SMB: check user name, password and domain in the storage settings
- NFS: check that the export allows this Docker host's IP address (/etc/exports on the server, then exportfs -ra)
- Then click Check on the storage again

### The NFS/SMB share could not be mounted: the server is not reachable or the share does not exist {#mount-failed}

Log lines like: `mount.nfs: Connection timed out`, `failed to mount local volume`

- Check the server address and the share path in the storage settings
- Check that the Docker host reaches the server (ping; NFS port 2049, SMB port 445 open)
- Then click Check on the storage again

### Hugging Face refused access to the model {#hf-auth}

Log lines like: `Access to model ... is restricted`, `401 Client Error: Unauthorized for url: https://huggingface.co/...`

- Gated models: accept the license on the model's Hugging Face page with your account
- Set a Hugging Face access token (SLOTSERVE_HF_TOKEN, read permission) and retry
- Check the repository name (private or renamed repositories also answer 401/404)

### No permission to read the model files {#models-permission-denied}

Log lines like: `failed to open GGUF file '...' (Permission denied)`, `[Errno 13] Permission denied: '...'`

- Give the user that runs SlotServe and the engine read access to the models folder: chmod -R a+rX <models folder>
- In Docker the container user must be able to read it; on NFS/SMB check the uid/gid mapping and mount options (uid=, file_mode=)
- SELinux hosts: mount the folder with :z, or check the audit log

### The model file is damaged or incomplete {#model-corrupt}

Log lines like: `invalid magic characters`, `failed to read tensor info`, `data is not within the file bounds`

- Download the model again: an interrupted download is the usual cause (compare size or SHA-256 with the source)
- Split models (…-00001-of-00003.gguf): all parts must be there; load the first part
- Check that the file really is a GGUF model for llama.cpp (not a safetensors file renamed)

### This engine version does not support the model's format or architecture {#model-unsupported}

Log lines like: `unknown model architecture: '...'`, `Model architectures ['...'] are not supported`

- Update the engine: new model architectures need a recent llama.cpp release or vLLM version
- Or use another build of the model (another quantization, or the GGUF from a different publisher)
- llama.cpp loads GGUF files; vLLM loads Hugging Face model folders (safetensors)

### The model file is missing or cannot be opened {#model-file-missing}

Log lines like: `failed to open GGUF file '...'`, `does not appear to have a file named config.json`

- Check that the file still exists (it may have been moved, renamed or deleted) and scan the models folder again
- Models on NFS/SMB: check that the share is mounted on this machine
- vLLM: the folder must contain config.json and the weights

### The model did not finish loading in time {#load-timeout}

Log lines like: `model did not become ready within ...s`

- Large models on slow disks or network storage take longer: raise SLOTSERVE_LOAD_TIMEOUT_SECONDS
- Look at the end of the log: a server that hangs without progress usually has another error there
- Copy the model to a local SSD for faster loading

### A file or folder that is needed does not exist {#file-not-found}

Log lines like: `No such file or directory`, `The system cannot find the file specified`

- Check the path in the log: typos in launch arguments, moved model files or a folder that is not mounted
- Paths inside Docker differ from the host: use the path as the container sees it
- Retry after fixing the path
