# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Dev environments**: a booking can be a Docker container with the lane's GPUs and SSH (a bundled static Dropbear, public-key login only, a port from a configurable range), a persistent `/workspace` volume and the models read-only. Built-in PyTorch, Jupyter + PyTorch, TensorFlow and CUDA templates, custom templates from any image, and saving a running environment as an image. Images are pulled ahead of time; failures, logs and every environment are visible on the page. Off by default; needs the Docker socket (or Podman's Docker-compatible API).
- Dev environment **passwords**: everyone can set an environment password (stored only as a SHA-512-crypt hash, written into the container) and log in with it as well as with SSH keys; admins can switch password logins off. Dropbear limits each connection to three tries. A booking needs a key or a password.
- **Access credentials** page (formerly API keys): API keys, SSH public keys and the environment password in one place.
- **What an idle lane runs** (Admin → GPUs & lanes): its default model, a **shared dev environment** everyone can log into with their own account (keys, password, kept home directory; stopped ahead of each booking and started again afterwards), or nothing. The overview shows what an idle lane is doing.
- **Kept dev environments**: a booking can keep its environment; the container is paused when the booking ends and a later booking continues it (recreated from a snapshot when the GPUs change). Rename, delete (optionally with its data) and limit them; unused ones can be removed after a number of days.
- **Kept directories** besides `/workspace` per template or kept environment (e.g. `/root`, `/opt/conda`), and an optional **storage directory** on the Docker host to keep persistent data in bind mounts instead of Docker volumes.

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
