# Contributing to SlotServe

Thanks for your interest! Bug reports, ideas and pull requests are all welcome. 欢迎用中文或英文提交 issue 和 PR。

## Reporting bugs

Open an issue using the **Bug report** template. Please include:

- what you did, what you expected, and what happened;
- SlotServe version (Settings → About), GPU, driver/CUDA version, llama.cpp commit;
- relevant lines from `docker compose logs` and **Models → Server log**.

Security problems: see [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

No GPU is required - the test suite and local runs use `tools/mock_llama_server.py`.

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest

# frontend (in another terminal)
cd frontend
npm install
npm run dev          # proxies /api and /v1 to http://127.0.0.1:8000
```

See [docs/development.md](docs/development.md) for running the backend against the mock server.

## Pull requests

1. Fork and create a branch from `main`.
2. Keep changes focused; one topic per PR.
3. Add or update tests for backend behaviour (`backend/tests`).
4. Make sure these pass:
   ```bash
   ruff check . && ruff format --check .
   (cd backend && pytest)
   (cd frontend && npm run typecheck && npm run build)
   pytest e2e        # end-to-end, needs the built frontend and `playwright install chromium`
   ```
   CI runs the same checks on every pull request. `pre-commit install` runs ruff before each commit.
5. User-facing text needs both languages. New features keep theirs in `frontend/src/features/<feature>/i18n.ts`
   (see [its README](frontend/src/features/README.md)); shared texts live in `frontend/src/i18n/en.ts` and `zh-CN.ts`.
   Docs pages have an English `*.md` and a Chinese `*.zh.md` version.
6. Describe user-visible changes in a changelog fragment, `changes/unreleased/<branch-or-topic>.md`, not in
   `CHANGELOG.md` itself (see [changes/README.md](changes/README.md)); `python tools/release_notes.py check`
   validates it.

## Working in parallel

Several branches are usually in flight at once. To keep them from colliding:

- **Add files instead of growing shared ones.** Changelog entries go into `changes/unreleased/`, end-to-end tests
  into a new `e2e/test_<feature>.py` (shared helpers in `e2e/helpers.py`; the modules share one server, see
  `MODULE_ORDER` in `e2e/conftest.py`), frontend code into `frontend/src/features/<feature>/`.
- **Imports point inwards.** `services`, `runtime`, `cluster` and `gateway` never import `api` (the HTTP layer);
  `backend/tests/test_layering.py` reports violations (report-only for now, it will fail once the existing ones
  are gone).
- **No "... hook" anchor comments.** If a change needs one, the shared file is missing an extension point; add the
  extension point instead.
- A pull request that touches more than three shared files should first split out the interface change.

### Ownership

Who reviews changes to which area, and how other changes use it. Some of the modules are planned and do not
exist yet.

| Area | Owner | How others use it |
| --- | --- | --- |
| `containers/*`, `services/devenv/{constants,scripts,templates,validation,ssh}.py` | container spec owner (`harden.py` / `helpers.py` also need the security owner) | a new contributor file plus one line in `build.py` |
| `services/devenv/{lifecycle,projects,shared,images,networking,node_access,manager}.py`, `cluster/nodeworker.py` | dev environment lifecycle owner | submit an operation |
| `services/placement.py`, allocations, node capacity | placement owner | `fit` / `reserve` / `release` / `dry_run` / `policy_for` |
| `services/app_settings.py`, `services/audit.py`, `services/reservations.py` | settings owner | register an `Editable` (one line) |
| `services/health.py`, `services/diagnostics/*`, delivery in `notify.py`, `maintenance.py`, `housekeeper.py` | operations owner | `register_check` / `heartbeat` / `report` |
| `security/*`, `deps.py`, `api/auth.py`, `api/keys.py`, `gateway/proxy.py::authenticate`, `gateway/devenv_web*.py`, the middleware part of `main.py`, the argument policy in `runtime/engines.py` | security owner | `credentials.issue` / `verify` / `revoke`, `Depends(sudo_user)` |
| `db.py`, `migrations/*`, `tests/fixtures/db/` | migrations owner | the pull request adds an idempotent migration function |
| `cluster/relay.py`, `agent/relay.py`, `services/devenv_relay.py`, SSH gateway, web port pool | relay owner | `Relay.open` / `RelayListener` |
| `cluster/protocol/*`, `capabilities.py`, `hub.py`, `agent/node.py`, `agent/handlers/*`, `cluster/install.py`, `resources/install/*`, `agent/docker_guard.py` | cluster owner | interface-first pull request that states the checks on the agent side |
| node state machine, interruptions, maintenance mode | node lifecycle owner | `report` and reading the state |
| `services/docker_endpoints.py`, `tests/contract/*`, `tests/unit/*`, `tools/verify/*` | test infrastructure owner | a new capability of a fake comes with a contract test |
| `features.py`, `AppContext`, the aggregation in `frontend/src/features/`, `AppLayout.vue`, `router.ts`, `api/client.ts` | platform owner / frontend platform owner | one registration line |
| `.github/workflows/*`, `docs/release-checklist.md`, the verification matrix | release owner (a human maintainer) | tag rules in `release.yml` change only with the maintainer |

## Style

- Python: formatted and linted with ruff (`ruff.toml`), type hints, small functions, standard library first.
- Vue: `<script setup lang="ts">`, Composition API, Naive UI components, colours from the CSS variables in `src/styles/main.css`.
- Commit messages: short imperative summary (`Add download retry button`), with details in the body if needed.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
