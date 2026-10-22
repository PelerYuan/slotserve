# 开发

开发 SlotServe 不需要 GPU。`tools/mock_llama_server.py` 模拟推理服务：接受 llama-server、vLLM 和 SGLang 的核心参数，假装加载几秒，然后提供支持流式和用量统计的 OpenAI 兼容回显模型。

## 目录结构

```text
backend/            FastAPI 应用（Python 包 `slotserve`）
  slotserve/api/      Web API 路由（/api）
  slotserve/gateway/  OpenAI 兼容代理（/v1）
  slotserve/runtime/  推理进程管理、推理后端、通道、调度器、GPU 状态
  slotserve/services/ 公共查询、模型扫描、下载队列
  tests/              pytest 测试（使用 mock 服务）
frontend/           Vue 3 + TypeScript + Naive UI
tools/              mock 推理服务
e2e/                Playwright 端到端测试
docs/               本文档站（MkDocs Material，中英双语）
```

## 后端

```bash
cd backend
python -m venv .venv && . .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

在仓库根目录用如下 `.env` 搭配 mock 运行：

```bash
SLOTSERVE_INVITE_CODE=dev
SLOTSERVE_SECRET_KEY=dev
SLOTSERVE_DATA_DIR=./dev/data
SLOTSERVE_MODELS_DIR=./dev/models
SLOTSERVE_LLAMA_SERVER_CMD=backend/.venv/bin/python tools/mock_llama_server.py
```

```bash
mkdir -p dev/models && truncate -s 5M dev/models/demo-Q4_K_M.gguf   # 任意以 .gguf 结尾的文件
backend/.venv/bin/slotserve
```

`MOCK_LOAD_SECONDS`（默认 5）和 `MOCK_TOKEN_DELAY`（默认 0.03）可调整 mock 行为。

## 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 与 /v1 代理到 :8000
npm run build      # 类型检查并输出到 backend/slotserve/static
```

## 文档

```bash
pip install -r docs/requirements.txt
mkdocs serve
mkdocs gh-deploy   # 发布到 GitHub Pages
```

## 约定

- Web API 错误格式为 `{"detail": {"code": "...", "params": {...}}}`，前端负责翻译 `code`（`frontend/src/i18n`）。新增错误码时请同时添加中英文文案。
- 网关错误遵循 OpenAI 错误格式，使用英文。
- 所有时间以 UTC 存储和传输，由浏览器按本地时区显示。

## 持续集成

每次推送到 `main` 和每个 Pull Request 都会运行 `.github/workflows/ci.yml`：

| 任务 | 检查内容 |
| --- | --- |
| Lint | `ruff check` 和 `ruff format --check` |
| 后端测试 | 在 Python 3.12 和 3.13 上运行 `pytest` |
| 前端 | `npm run typecheck` 和 `npm run build` |
| 端到端 | 启动 SlotServe 和模拟推理服务，用 Playwright 操作界面（`e2e/`） |
| 文档 | `mkdocs build --strict` |
| Docker 镜像 | 构建镜像并确认 `/healthz` 可访问 |

CodeQL 每周以及在 PR 上扫描 Python 和 TypeScript。Dependabot 每周为 pip、npm、GitHub Actions 和 Docker 基础镜像提交更新 PR（不会自动升级 CUDA 大版本）。

## 发布流程

变更记录写在片段文件里（`changes/unreleased/*.md`，见 `changes/README.md`），发布时由 `tools/release_notes.py` 合并进 `CHANGELOG.md`。包括候选版在内的完整流程见[发版清单](release-checklist.md)（英文）。

1. 更新 `backend/pyproject.toml`、`backend/slotserve/__init__.py`、`frontend/package.json` 中的版本号，并打候选版标签：`git tag v0.10.0-rc1 && git push origin v0.10.0-rc1`。工作流只推送 `0.10.0-rc1` 和 `rc`（不会推 `latest`），并创建 GitHub 预发布。
2. 候选版验证通过后：`python tools/release_notes.py release 0.10.0`，提交，再 `git tag v0.10.0 && git push origin main v0.10.0`。
3. `.github/workflows/release.yml` 会构建并推送 `ghcr.io/peleryuan/slotserve`（`0.10.0`、`0.10`、`latest`），并用 CHANGELOG 中对应的段落创建 GitHub Release。

`main` 上的 `docs/` 或 `mkdocs.yml` 变化时，文档站会自动部署。
