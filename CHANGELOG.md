# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/PelerYuan/slotserve/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/PelerYuan/slotserve/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/PelerYuan/slotserve/releases/tag/v0.1.0
