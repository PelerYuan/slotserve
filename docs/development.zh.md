# 开发

开发 SlotServe 不需要 GPU。`tools/mock_llama_server.py` 模拟 llama-server：接受相同的核心参数，假装加载几秒，然后提供支持流式和用量统计的 OpenAI 兼容回显模型。

## 目录结构

```text
backend/            FastAPI 应用（Python 包 `slotserve`）
  slotserve/api/      Web API 路由（/api）
  slotserve/gateway/  OpenAI 兼容代理（/v1）
  slotserve/runtime/  llama-server 进程管理、调度器、GPU 状态
  slotserve/services/ 公共查询、模型扫描、下载队列
  tests/              pytest 测试（使用 mock 服务）
frontend/           Vue 3 + TypeScript + Naive UI
tools/              mock llama-server
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
