# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/PelerYuan/slotserve/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PelerYuan/slotserve/releases/tag/v0.1.0
