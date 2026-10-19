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
   (cd backend && pytest)
   (cd frontend && npm run build)
   ```
5. User-facing text needs both languages: `frontend/src/i18n/en.ts` and `zh-CN.ts`. Docs pages have an English `*.md` and a Chinese `*.zh.md` version.
6. Update `CHANGELOG.md` under **Unreleased**.

## Style

- Python: type hints, small functions, standard library first. Follow the surrounding code.
- Vue: `<script setup lang="ts">`, Composition API, Naive UI components, colours from the CSS variables in `src/styles/main.css`.
- Commit messages: short imperative summary (`Add download retry button`), with details in the body if needed.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
