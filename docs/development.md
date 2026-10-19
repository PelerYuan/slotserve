# Development

You do not need a GPU to work on SlotServe. `tools/mock_llama_server.py` imitates llama-server: it accepts the same core flags, pretends to load for a few seconds and serves an OpenAI-compatible echo model with streaming and usage.

## Layout

```text
backend/            FastAPI application (Python package `slotserve`)
  slotserve/api/      Web API routes (/api)
  slotserve/gateway/  OpenAI-compatible proxy (/v1)
  slotserve/runtime/  llama-server process manager, scheduler, GPU stats
  slotserve/services/ shared queries, model scanner, download queue
  tests/              pytest suite (uses the mock server)
frontend/           Vue 3 + TypeScript + Naive UI
tools/              mock llama-server
e2e/                Playwright end-to-end tests
docs/               this site (MkDocs Material, en + zh)
```

## Backend

```bash
cd backend
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Run it against the mock from the repository root with a `.env` like:

```bash
SLOTSERVE_INVITE_CODE=dev
SLOTSERVE_SECRET_KEY=dev
SLOTSERVE_DATA_DIR=./dev/data
SLOTSERVE_MODELS_DIR=./dev/models
SLOTSERVE_LLAMA_SERVER_CMD=backend/.venv/bin/python tools/mock_llama_server.py
```

```bash
mkdir -p dev/models && truncate -s 5M dev/models/demo-Q4_K_M.gguf   # any file ending in .gguf
backend/.venv/bin/slotserve
```

`MOCK_LOAD_SECONDS` (default 5) and `MOCK_TOKEN_DELAY` (default 0.03) tune the mock.

## Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api and /v1 to :8000
npm run build      # type-checks and writes to backend/slotserve/static
```

## Docs

```bash
pip install -r docs/requirements.txt
mkdocs serve
mkdocs gh-deploy   # publish to GitHub Pages
```

## Conventions

- Web API errors are `{"detail": {"code": "...", "params": {...}}}`; the frontend translates `code` (`frontend/src/i18n`). Add both English and Chinese strings for new codes.
- Gateway errors follow the OpenAI error format and are in English.
- All timestamps are stored and sent as UTC; the browser formats them in local time.

## Continuous integration

Every push to `main` and every pull request runs `.github/workflows/ci.yml`:

| Job | What it checks |
| --- | --- |
| Lint | `ruff check` and `ruff format --check` |
| Backend tests | `pytest` on Python 3.12 and 3.13 |
| Frontend | `npm run typecheck` and `npm run build` |
| End-to-end | starts SlotServe with the mock llama-server and drives the UI with Playwright (`e2e/`) |
| Docs | `mkdocs build --strict` |
| Docker image | builds the image and checks that `/healthz` answers |

CodeQL scans Python and TypeScript weekly and on pull requests. Dependabot opens weekly update PRs for pip, npm, GitHub Actions and the Docker base image (CUDA major versions are not bumped automatically).

## Releasing

1. Move the **Unreleased** entries in `CHANGELOG.md` under a new version heading and bump the version in `backend/pyproject.toml`, `backend/slotserve/__init__.py` and `frontend/package.json`.
2. Commit, then tag and push: `git tag v0.5.0 && git push origin main v0.5.0`.
3. `.github/workflows/release.yml` builds and pushes `ghcr.io/peleryuan/slotserve` (`0.5.0`, `0.5`, `latest`) and creates the GitHub release from the changelog section.

The documentation site deploys automatically when `docs/` or `mkdocs.yml` change on `main`.
