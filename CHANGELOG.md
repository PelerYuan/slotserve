# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Compute usage**: every minute SlotServe records each GPU's utilization, memory and power together with what it was doing (model booking, whole-GPU booking, dev environment, shared idle use, or nothing) and for whom. Works without GPU metrics too (occupancy only). Per-minute samples are folded into hourly totals after `usage_sample_days` (30); `usage_busy_percent` (10) sets when a GPU counts as used. Both are editable in **Admin → System settings**.
- The **Usage** page is now about GPU time: booked vs. used GPU-hours, average utilization while booked ("booked but idle" made visible), energy, peak memory and tokens; GPU-hours over time by person or by kind; utilization per lane; tables by person, lane (with occupancy), kind, GPU and model; **Everyone** / **Just me**.
- CSV exports per person and of GPU time per day, lane, GPU, kind and person; all exports take `scope=mine`.
- Prometheus: GPU gauges carry a `lane` label; new `slotserve_lane_occupant`, `slotserve_booked_gpu_hours_total`, `slotserve_gpu_seconds_total`, `slotserve_gpu_busy_seconds_total`, `slotserve_gpu_energy_joules_total` and `slotserve_user_gpu_busy_seconds_total`.

- **Dev environments**: a booking can be a Docker container with the lane's GPUs and SSH (a bundled static Dropbear, public-key login only, a port from a configurable range), a persistent `/workspace` volume and the models read-only. Built-in PyTorch, Jupyter + PyTorch, TensorFlow and CUDA templates, custom templates from any image, and saving a running environment as an image. Images are pulled ahead of time; failures, logs and every environment are visible on the page. Off by default; needs the Docker socket (or Podman's Docker-compatible API).
- **Web interfaces in dev environments**: SlotServe reverse-proxies any port of a running environment at `/devenv/<session>/port/<port>/` (HTTP streaming and WebSockets), for the owner and admins (and everyone who can log into a shared idle environment). Templates declare their web interfaces as a list of ports (name, port, kind: desktop, JupyterLab, VS Code or any web app), each with an **Open** button on the environment's card. Each environment gets its own Docker network; web ports are never published (SlotServe joins the network when it runs in a container, otherwise uses ports bound to 127.0.0.1 or the container's network address). A random token per start is passed to JupyterLab and the desktop and added by the proxy. Placeholders `{base_path}`, `{base_path_<port>}`, `{token}`, `{port}` in the command and environment; an option keeps the image's own entrypoint and starts SSH with `docker exec`. New built-ins: Ubuntu desktop (linuxserver webtop XFCE) and VS Code (code-server); the Jupyter template now opens JupyterLab directly.
- Dev environment **passwords**: everyone can set an environment password (stored only as a SHA-512-crypt hash, written into the container) and log in with it as well as with SSH keys; admins can switch password logins off. Dropbear limits each connection to three tries. A booking needs a key or a password.
- **Access credentials** page (formerly API keys): API keys, SSH public keys and the environment password in one place.
- **What an idle lane runs** (Admin → GPUs & lanes): its default model, a **shared dev environment** everyone can log into with their own account (keys, password, kept home directory; stopped ahead of each booking and started again afterwards), or nothing. The overview shows what an idle lane is doing.
- **Kept dev environments**: a booking can keep its environment; the container is paused when the booking ends and a later booking continues it (recreated from a snapshot when the GPUs change). Rename, delete (optionally with its data) and limit them; unused ones can be removed after a number of days.
- **Kept directories** besides `/workspace` per template or kept environment (e.g. `/root`, `/opt/conda`), and an optional **storage directory** on the Docker host to keep persistent data in bind mounts instead of Docker volumes.
- **Dev environments without a GPU**: anyone can start a template or a kept environment on the CPU at any time, without a booking, for editing code, moving data, installing packages and CPU jobs; it runs until its owner pauses it. A kept environment running without a GPU moves to the GPUs when one of its bookings starts (snapshot and recreate) and is paused afterwards, or continues without a GPU when its owner switched that on. Each GPU lane reserves CPU cores for its bookings and shared environment (cpuset, **CPU cores reserved per GPU**, default 8); environments without a GPU share the other cores with equal weight, per-environment core and memory limits, a per-person limit and an optional memory pool. Host cores and memory come from Docker. The page shows live CPU and memory use; admins see and can pause every one. Their CPU time is recorded as CPU core-hours on the **Usage** page.
- **More chat platforms**: besides Telegram, the bot runs on Feishu / Lark (self-built app, long connection, interactive cards), DingTalk (internal app robot, Stream mode), WeCom (self-built app; sending via the API, commands through a signed and encrypted callback URL) and QQ (official bot, WebSocket gateway, private chats and @mentions in groups). Every platform has the same commands and booking flow; platforms without buttons get numbered choices (on DingTalk, links that send the number). Commands also work without the `/` and in Chinese.
- People link each platform in **Settings → Chat notifications** with a one-time bind code (Telegram keeps its deep link), can pause each platform, and get every notification on every platform they linked; which notifications they get is still chosen once.
- **Group webhooks** for Feishu / Lark (optional signature), DingTalk (signed) and WeCom: admins choose whether groups get admin alerts, approval requests and announcements, and can post announcements and send tests from **Admin → Chat channels**, which also shows whether each bot is connected, its errors, how many people linked it and the WeCom callback URL.

- **Storage managed in the panel** (Admin → Storage): host directories, NFS exports and SMB/CIFS shares (password stored encrypted, never returned), mounted through Docker named volumes of the `local` driver so the host needs nothing but `nfs-common`/`cifs-utils`. Each storage has a purpose - **shared data** (mounted at a chosen path, optionally read-only), **personal spaces** (each person's folder becomes `/workspace` instead of a Docker volume; shared idle environments see everyone's folder under `/workspaces`, each private to its owner), **project data** (kept directories of kept environments and templates; takes over from the storage directory setting, which keeps working) or a **model library** (scanned for models, a download target via `storage_id`, optionally mounted into environments) - and is given to everyone, chosen people or admins only. Folders are created and seeded by helper containers; kept environments are recreated from a snapshot when their storages change.
- Storage **checks**: reachable or the mount error, free/total space, writable, per-person usage of personal spaces (`du`), run every `SLOTSERVE_STORAGE_CHECK_MINUTES` (30) in a `SLOTSERVE_STORAGE_HELPER_IMAGE` (`busybox:1.37`) container; optional soft limit per person with notifications to the person and admins; admins are told when a storage stops working. **Dev environments** shows everyone the storages they get and their usage.
- **Ports of dev environments**: each running environment's card lists its ports (name, container port, how it opens, address with Open and Copy, and whether something listens on it, probed inside the container). Ports can be added, switched and removed while the environment runs, without restarting it; listening ports that are not listed yet are offered with one click. Shared idle environments only list their template's ports.
- **Direct ports**: a port can be published as `http://server:<host port>` for any app or protocol, without the SlotServe login, through a small forwarder container (`alpine/socat`) on the environment's network. Host ports come from `devenv_public_ports` (2300-2399); a kept environment keeps its numbers while paused and gets them back when continued, other environments release them when the booking ends. Admins set `devenv_public_port_limit` (5 per person; 0 = only admins), switch direct ports off, and see and reclaim everyone's direct ports on the Dev environments page. Templates can declare a port as direct; saving an environment as an image keeps its port declarations.

### Changed

- **Dev environment web interfaces moved to their own origin** (security): they are served on a separate port (`SLOTSERVE_DEVENV_WEB_PORT`, default 8001, a second listener in the same process; `docker-compose.yml` publishes it), so a page inside someone's environment can no longer act in SlotServe with the rights of whoever opens it. SlotServe hands people over with a one-time token that becomes an HttpOnly cookie for that one environment; SlotServe's own cookie is never accepted there, SlotServe refuses changing requests from that origin, and one environment's pages cannot open another's. Links and bookmarks of `/devenv/<session>/port/<port>/` on SlotServe's address keep working as redirects. Behind a reverse proxy set `SLOTSERVE_DEVENV_WEB_URL`; `SLOTSERVE_DEVENV_WEB_PORT=0` restores the old behaviour.
- Notifications and the booking bot no longer depend on Telegram: commands and buttons are handled once for all platforms. Undelivered messages are retried per platform.
- **Web interfaces in dev environments**: SlotServe reverse-proxies any port of a running environment at `/devenv/<session>/port/<port>/` (HTTP streaming and WebSockets), for the owner and admins only. Templates declare their web interfaces as a list of ports (name, port, kind: desktop, JupyterLab, VS Code or any web app), each with an **Open** button on the environment's card. Each environment gets its own Docker network; web ports are never published (SlotServe joins the network when it runs in a container, otherwise uses ports bound to 127.0.0.1 or the container's network address). A random token per start is passed to JupyterLab and the desktop and added by the proxy. Placeholders `{base_path}`, `{base_path_<port>}`, `{token}`, `{port}` in the command and environment; an option keeps the image's own entrypoint and starts SSH with `docker exec`. New built-ins: Ubuntu desktop (linuxserver webtop XFCE) and VS Code (code-server); the Jupyter template now opens JupyterLab directly.
- **Model catalog** (Models → Catalog): about 35 popular open models for single servers - chat, coding, reasoning, embeddings and vision-language, several sizes each - with publisher, licence, parameters, context and descriptions in English and Chinese. Variants are GGUF files for llama.cpp (exact files, split parts and vision projectors) and safetensors repositories for vLLM / SGLang, all verified against the Hugging Face API with exact sizes (`tools/update_model_catalog.py` re-checks them). Each variant shows an estimate of the GPU memory it needs (weights + KV cache for the chosen context + overhead) and whether it fits on each lane. Filters for task, size, engine and lane, and search.
- **One-click deploy** from the catalog: pick a variant, engine, context and lane; the files go through the download queue (Hugging Face endpoint and token, disk-space check, resume) and the model is registered with launch arguments for its engine (`-ngl 99 -c`, `--mmproj`, `--max-model-len`, tensor parallelism on multi-GPU lanes, embedding flags), optionally as a lane's default. Files already on disk are registered at once; models already registered are reused.
- Admins can hide built-in catalog entries and add their own (file sizes, split parts and architecture are looked up on Hugging Face).

### Fixed

- **Add model** on the Models page saved nothing.

## [Unreleased: multiple machines] (feature branch, not released yet)

### Added

- **Multiple machines**: one SlotServe server can use the GPUs (or CPUs) of several machines. Each extra machine runs a node agent (`slotserve agent --server URL --token TOKEN`, or `SLOTSERVE_AGENT_SERVER` / `SLOTSERVE_AGENT_TOKEN`; same Docker image, see `docker-compose.node.yml`) that connects out to the server over a WebSocket with its own token. The server is the built-in node, so single-machine installations behave exactly as before.
- **Admin → Nodes**: add a machine (token shown once, with a ready `docker run` command), rename it, set the address the server uses to reach it, issue a new token, revoke and delete; status, last seen, agent and protocol versions, hostname, CPU cores and load, memory, GPUs, models folder and lanes per machine.
- Lanes belong to a machine (**Admin → GPUs & lanes**); GPUs and overlaps are checked per machine. Model servers on a node are started and stopped by its agent; loading progress, the live log, log download, failure details and load history work in the runtime monitor as for local lanes. The gateway forwards requests directly to the node.
- Models are per machine: **Scan models** also scans every connected node (or one node from Admin → Nodes); downloads can go straight to a node (**Download to**). A lane only serves, and can only be booked with, models its machine has; shared storage makes a model available on every machine that sees the same file.
- When a node goes offline its lanes become unavailable (the gateway stops routing to them, "Offline" in the UI); admins and people with bookings on it in the next 24 hours are notified, admins again when it is back. On reconnect the server takes over what the node is running without restarting it and stops servers of lanes that were removed meanwhile. Incompatible agent versions are refused with a clear message.
- Usage sampling records remote lanes with the GPU metrics their agents report.
- New settings: `SLOTSERVE_LOCAL_NODE_NAME`, `SLOTSERVE_NODE_HEARTBEAT_SECONDS`, `SLOTSERVE_NODE_OFFLINE_SECONDS`, `SLOTSERVE_NODE_RPC_TIMEOUT_SECONDS`; for agents `SLOTSERVE_AGENT_SERVER`, `SLOTSERVE_AGENT_TOKEN`, `SLOTSERVE_AGENT_ADDRESS`, `SLOTSERVE_AGENT_MODEL_HOST`. Docs: "Multiple machines".

- **Personal computers**: one-line node installers served by the server (`/install/node.sh` for Linux and macOS, `/install/node.ps1` for Windows with Docker Desktop and WSL 2), shown with the token in Admin → Nodes; they check Docker, the NVIDIA Container Toolkit / WSL 2 GPU support and the network card, then start the agent with a restart policy. Agents report their platform (e.g. *Windows · WSL2 (Docker Desktop)*, macOS as a CPU-only node); nvidia-smi is also found under WSL's `/usr/lib/wsl/lib`.
- **Owners**: a node can belong to a person (Admin → Nodes). Owners (and admins) set weekly **sharing hours** in the server's time zone (overnight windows, "nights and weekends" preset, always or never), **pause sharing** ("I'm using my PC") with a grace period (`sharing_grace_minutes`, 10) after which others' running bookings end (kept dev environments are paused, not deleted), see everything running on their machine and stop it, on the new **My machines** page. Bookings by others must lie inside the sharing hours and outside a pause (`outside_sharing_hours`, `node_sharing_paused`); outside the hours lanes run no default model or shared environment; the calendar hatches unavailable hours. A schedule change that conflicts with bookings notifies the owner and the people affected.
- **Wake-on-LAN**: MAC addresses (reported by agents, passed by the installers or entered by hand) and a network label per node; before a booking on an offline node starts (`wol_lead_minutes`, 5) an online machine in the same network (same label or subnet, or the server itself) sends the magic packet (agent RPC `wol.send`); **Wake now** button; the booker and the owner are told when the machine is still offline at the start.
- **Contribution**: per person, hours their machines were online and shared, GPU-hours offered and used by others, and GPU-hours they used; on the Usage page (CSV) and per machine. Docs: "Personal computers".

### Not yet

- Dev environments (containers, SSH, web ports, environments without a GPU) still run on the server's machine only.
- One-click deploys from the model catalog and downloads into model libraries (Admin → Storage) go to the server's machine only; a catalog deploy to a lane on another node is refused (`catalog_remote_lane`), as is a library download on another node (`storage_remote_node`).

## [0.8.0] - 2026-10-21

### Added

- **Admin → Runtime monitor**: per-lane live server output (server-sent events), loading stage and progress read from llama.cpp / vLLM / SGLang output, failure details (error, exit code, last output lines, retry countdown), the exact launch command and a load history with command and output excerpt for every load.
- The overview's loading bar uses the progress the model server reports when available.
- Admin switch **Let people view their API keys again**: new keys keep an encrypted copy (Fernet, keyed from `SECRET_KEY`) and owners can **View** them; switching it off deletes every copy. Off by default.
- Light/dark switch in the user menu, the mobile menu and on the sign-in page.
- Telegram: `/book` books GPU time with buttons (lane, model or whole GPU, day, start, length) under the same rules as the web, offering the waitlist when the time was just taken; `/free` lists free time for today and tomorrow; `/cancel` cancels an upcoming booking. The bot registers its command menu.

## [0.7.1] - 2026-10-21

### Added

- **Admin → GPUs & lanes**: per-lane **Reload** and **Arguments** (edit the running model's launch arguments and reload the lanes using it).

### Changed

- Documentation brought up to date for lanes, engines and approval; architecture, scheduling, routing and booking-status diagrams are drawn with Mermaid.
- The sidebar no longer shows the GPU status card; the overview page has it.
- The sign-in page keeps its content next to the divider on wide screens.
- `.env.example` only sets deployment values; settings admins change at runtime are listed as optional.

## [0.7.0] - 2026-10-20

### Added

- Multiple GPUs: admins split GPUs into lanes, each with its own model server (`CUDA_VISIBLE_DEVICES`), scheduler, default model and booking calendar. The gateway routes requests to the right lane; single-GPU setups look exactly as before.
- Inference engines per model: llama.cpp, vLLM, SGLang, or a custom command template. Models can be added by hand; `Dockerfile.vllm` builds SlotServe on the vLLM image.
- Approval mode (off / long bookings / all): pending bookings hold their time, admins approve or reject on the web or from Telegram, and unreviewed requests expire.

### Changed

- Metrics `slotserve_runtime_state`, `slotserve_runtime_model_info` and `slotserve_reservation_active` carry a `lane` label.
- `POST /api/models/{id}/default` takes an optional `lane_id`; the default model is now a property of a lane.

### Fixed

- Notifications whose text contains `{user}` (fallback alerts for admins) failed to send.

## [0.6.0] - 2026-10-20

### Added

- Minimum preload time (`SLOTSERVE_PRELOAD_MIN_MINUTES`, default 3 minutes): a booked model starts loading at least this early even when its learned load time is shorter.
- Every operational setting can be changed in **Admin → System settings**: calendar slot, reminder lead time, preload timing, load and stop timeouts, disk read speed, scheduler interval and minimum free disk space. Only deployment settings stay in `.env`.
- The settings page shows units, ranges and choices from the server, and lists new settings automatically.

## [0.5.0] - 2026-10-20

### Added

- Continuous integration: lint, backend tests on Python 3.12 and 3.13, frontend typecheck and build, Playwright end-to-end tests, strict docs build and a Docker image build with a health check.
- Release workflow: tags publish `ghcr.io/peleryuan/slotserve` and create the GitHub release from this changelog.
- Docs deploy automatically; CodeQL scanning; Dependabot updates.
- `docker-compose.yml` can use the prebuilt image (`docker compose pull`).
- ruff configuration, pre-commit hooks and CODEOWNERS.

### Changed

- Python 3.12 or newer is required.

## [0.4.0] - 2026-10-20

### Added

- Model switches wait for running requests to finish (`SLOTSERVE_DRAIN_SECONDS`); new requests get `503 model_switching` meanwhile.
- Models larger than GPU memory are flagged, and out-of-memory load failures explain how to fix them.
- Sign-in throttling: 10 failures per account or 30 per address in 15 minutes return `429`.
- Daily database backups with a backup list, on-demand backup and download in the admin overview.
- Retention: per-request rows older than `SLOTSERVE_USAGE_RETENTION_DAYS` are folded into daily totals; old audit, download, waitlist and load records are cleaned up.
- Undelivered Telegram messages are retried with back-off; pending notifications and the admin's manual model choice survive restarts.

### Changed

- `slotserve_requests_total` is labelled by `outcome` (`ok`/`error`) instead of the HTTP status, and includes archived totals.

## [0.3.0] - 2026-10-20

### Added

- Whole-GPU bookings: llama-server is stopped for the slot so the GPU can run training or other programs.
- Launch-argument presets per model, selectable when booking.
- Edit upcoming bookings, extend or shorten running ones, and weekly repeating bookings.
- Admins can book for someone else and edit any booking.
- Waitlist with automatic booking when the time frees up.
- Per-person quotas: hours per week and open bookings.
- Admin manual control: load a model or unload now, for a set time or until the next booking.
- Automatic fallback to the default model while a booked model fails to load.
- Telegram bot with reminders, readiness and change notifications, inline extend/end buttons and commands.
- Private iCalendar feeds for your own or everyone's bookings.
- Usage export as CSV (daily summary or every request).
- Admin request log.
- Hugging Face repository browser for downloads, with mirror endpoint and token support.
- Prometheus metrics at `/metrics`.
- Dark theme (system, light or dark).

### Changed

- Database schema is now versioned; upgrades back up the database file to `data/backups/` first.
- `POST /api/reservations` and `DELETE /api/reservations/{id}` return a list of bookings.

## [0.2.0] - 2026-10-20

### Added

- Admin panel (`/admin`) with six tabs: overview, users, bookings, API keys, system settings and audit log.
- User management in the browser: create users without an invite code, reset passwords, grant or remove admin, disable, enable and delete accounts.
- Disabled accounts are signed out, their API keys are refused, and their open bookings are cancelled.
- System settings editable at runtime: open registration, invite code, longest booking, booking horizon and default launch arguments. Overrides are stored in the database; each can be reset to the `.env` value.
- Audit log of admin actions.
- `SLOTSERVE_REGISTRATION_OPEN` setting.
- Animated sign-in page with a live schedule demo and a sign-in / sign-up switch.

### Changed

- Existing databases gain new columns automatically on startup.

## [0.1.0] - 2026-10-20

### Added

- Calendar booking with exclusive, non-overlapping time slots; cancel and early release.
- OpenAI-compatible gateway (`/v1`) with per-user API keys, streaming pass-through and usage logging.
- Managed `llama-server` process: health checks, failure back-off, per-model launch arguments, log viewer.
- Scheduler that switches to the booked model and preloads ahead of bookings using learned load times.
- Free use of the default model whenever the GPU is not booked.
- Model directory scanning (split GGUF aware) and resumable URL downloads.
- Usage dashboard by person, model and day.
- GPU status via `nvidia-smi`.
- English and Simplified Chinese UI.
- Docker image and Compose file that mount a host-built llama.cpp.
- Admin CLI: `slotserve reset-password`, `slotserve set-admin`.
- Bilingual documentation site (MkDocs Material).

[Unreleased]: https://github.com/PelerYuan/slotserve/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/PelerYuan/slotserve/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/PelerYuan/slotserve/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/PelerYuan/slotserve/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/PelerYuan/slotserve/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/PelerYuan/slotserve/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/PelerYuan/slotserve/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/PelerYuan/slotserve/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/PelerYuan/slotserve/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/PelerYuan/slotserve/releases/tag/v0.1.0
