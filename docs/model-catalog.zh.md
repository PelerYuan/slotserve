# 模型目录

**模型 → 模型目录**列出适合在单台服务器上运行的热门开放模型，并支持一键部署。内置目录中的每个文件都已通过 Hugging Face API 核对，显示的大小是精确值。

## 包含哪些模型

约 35 个模型，分为五类，多数提供多种规格：

| 用途 | 示例 |
| --- | --- |
| 对话 / 指令 | Qwen3（4B–32B、30B-A3B）、Llama 3.1 8B / 3.2 3B、Gemma 3（4B、12B、27B）、Mistral Small 3.2、Phi-4、gpt-oss 20B / 120B |
| 编程 | Qwen2.5 Coder（7B、14B、32B）、Qwen3 Coder 30B-A3B、Devstral Small |
| 推理 | DeepSeek R1 蒸馏版（7B、14B、32B、Qwen3 8B）、QwQ 32B、Qwen3、Phi-4、gpt-oss |
| 向量 | Qwen3 Embedding（0.6B、4B、8B）、Nomic Embed Text v1.5、BGE-M3 |
| 视觉语言 | Qwen2.5 VL（3B、7B、32B）、Qwen3 VL（4B、8B、30B-A3B）、Gemma 3、Mistral Small 3.2 |

每个条目显示发布者、许可证、参数量（混合专家模型同时显示激活参数）、最大上下文和简介，以及一个或多个**版本**：

- **GGUF**（llama.cpp）：量化方式（Q4_K_M、Q8_0 等）、包括分卷在内的确切文件，视觉模型还包括视觉投影文件（`mmproj`）。
- **safetensors**（vLLM 或 SGLang）：原始权重或 FP8 / AWQ / MXFP4 版本。只下载引擎需要的文件（配置、分词器、权重），不下载原始检查点、其他运行时的格式或图片。

标有**需同意许可**的版本来自受限仓库：部署前请在该模型的 Hugging Face 页面同意许可，并在**管理 → 系统设置**中填写 **Hugging Face Token**。多数受限模型也有无需许可的 GGUF 版本。

可以按用途、大小、推理引擎和**可在某通道运行**筛选，也可以搜索名称、发布者、标签和量化方式。

## 放得下吗？

目录为每个版本估算处理一个请求所需的显存：

- **权重**：下载大小（加上视觉投影文件）；
- **KV 缓存**：`2 × 层数 × KV 头数 × 头维度 × 上下文 × 2 字节`（16 位缓存；对使用滑动窗口的模型是上限）；
- **开销**：llama.cpp 约 0.5 GB + 权重的 3%，vLLM / SGLang 约 1.5 GB + 5%（激活值、CUDA graph）。

估算值与每个通道所有 GPU 的显存总量（来自 `nvidia-smi`）比较：

| 标记 | 含义 |
| --- | --- |
| 可运行 | 低于通道显存的 95%（vLLM / SGLang 为 90%，它们会预留固定比例）。 |
| 勉强 | 刚好放得下：请缩短上下文或减少卸载到 GPU 的层数。 |
| 显存不足 | 需要的显存超过通道总量。llama.cpp 仍可部分在 CPU 上运行，但较慢。 |
| 未知 | 该通道没有 GPU 信息（没有 NVIDIA 驱动，或检测不到通道的 GPU）。 |

卡片上按通道显示能放下的最大版本。在部署对话框中修改上下文可以看到 KV 缓存的变化；并发请求（`--parallel` / `-np`）需要更多显存。

## 部署

在卡片上或**详情**中的某个版本上点**部署**，然后选择：

- **版本**，以及 safetensors 版本的**推理引擎**（vLLM 或 SGLang）；
- **上下文长度**（默认 8K tokens，模型支持的上下文更短时取更小值）；
- **通道**（用于判断能否运行；对多 GPU 通道上的 vLLM / SGLang 还会启用张量并行）；
- 可选的**模型名称**（默认 `<条目>-<版本>`，例如 `qwen3-8b-q4_k_m`）；
- （管理员）**设为该通道的默认模型**。

SlotServe 先检查磁盘能否容纳全部文件并满足**下载后至少保留**的空间，然后把文件加入普通的下载队列——因此会使用已配置的 Hugging Face 地址和 Token，下载可断点续传，并显示在**下载**列表中。GGUF 文件与 Hugging Face 标签页一样放在模型目录中；safetensors 仓库放在名为 `<组织>--<仓库>` 的文件夹中。目录页会显示每次部署的进度，最后一个文件下载完成后，模型会以默认启动参数登记：

| 引擎 | 参数 |
| --- | --- |
| llama.cpp | `-ngl 99 -c <上下文>`，视觉模型加 `--mmproj <文件>`，向量模型加 `--embedding --pooling ...` |
| vLLM | `--max-model-len <上下文>`，多 GPU 通道加 `--tensor-parallel-size <GPU 数>`，向量模型加 `--runner pooling` |
| SGLang | `--context-length <上下文>`，多 GPU 通道加 `--tp-size <GPU 数>`，向量模型加 `--is-embedding` |

部分条目会附加其他参数（例如 gpt-oss 的 `--jinja` 和 Mistral 分词器选项）。登记后可以像其他模型一样修改参数，预设也照常可用。引擎参数可能随版本变化；模型启动失败时请查看**运行日志**。

部署已有的模型不会重复下载：文件已在磁盘上时立即登记；已经登记过则直接使用现有模型（如有要求会设为通道默认）。某个文件下载失败时，在**下载**中重试即可，所有文件到齐后部署会继续完成。在目录页的部署列表中移除一条部署会取消其排队中的下载。

## 管理目录（管理员）

- 在内置条目的**详情**中点**从目录中隐藏**，所有人都将看不到它；打开**显示已隐藏**可以恢复。
- **添加条目**可创建自己的条目：名称、用途、参数量、上下文、中英文描述，以及以 Hugging Face 仓库加（GGUF）文件名表示的版本——分卷文件填第一个即可。SlotServe 会从 Hugging Face 获取文件大小和分卷，并读取原始仓库的 `config.json` 用于估算 KV 缓存。自己的条目可以编辑和删除，删除不会影响已安装的模型。

内置列表位于 `backend/slotserve/resources/model_catalog.json`。修改后（或要确认仍然准确时），在仓库根目录运行：

```bash
PYTHONPATH=backend python tools/update_model_catalog.py          # 核对并写入大小
PYTHONPATH=backend python tools/update_model_catalog.py --check  # 只报告
```

任何仓库或文件不存在时都会报错。设置 `HF_TOKEN` 可读取受限仓库，设置 `HF_ENDPOINT` 可使用镜像。

## 接口

| 方法 | 路径 | |
| --- | --- | --- |
| GET | `/api/catalog?ctx=` | 所有条目及其显存估算和各通道是否可运行（`ctx` 覆盖估算使用的上下文） |
| GET | `/api/catalog/{entry}?ctx=` | 单个条目 |
| POST | `/api/catalog/deploy` | `{entry_id, variant_id, engine?, lane_id?, set_default?, ctx?, name?}` |
| GET / DELETE | `/api/catalog/deploys`、`/api/catalog/deploys/{id}` | 最近的部署及进度；移除（取消）一条 |
| PUT | `/api/catalog/{entry}/hidden` | 管理员：`{hidden}` |
| POST / PUT / DELETE | `/api/catalog/entries`、`/api/catalog/entries/{entry}` | 管理员：自定义条目 |
