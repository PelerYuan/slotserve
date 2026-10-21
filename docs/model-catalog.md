# Model catalog

**Models → Catalog** lists popular open models that run well on a single server, with one-click deployment. Every file in the built-in catalog was checked against the Hugging Face API, and the sizes shown are exact.

## What is in it

About 35 models in five groups, most of them in several sizes:

| Task | Examples |
| --- | --- |
| Chat / instruct | Qwen3 (4B–32B, 30B-A3B), Llama 3.1 8B / 3.2 3B, Gemma 3 (4B, 12B, 27B), Mistral Small 3.2, Phi-4, gpt-oss 20B / 120B |
| Coding | Qwen2.5 Coder (7B, 14B, 32B), Qwen3 Coder 30B-A3B, Devstral Small |
| Reasoning | DeepSeek R1 distills (7B, 14B, 32B, Qwen3 8B), QwQ 32B, Qwen3, Phi-4, gpt-oss |
| Embeddings | Qwen3 Embedding (0.6B, 4B, 8B), Nomic Embed Text v1.5, BGE-M3 |
| Vision-language | Qwen2.5 VL (3B, 7B, 32B), Qwen3 VL (4B, 8B, 30B-A3B), Gemma 3, Mistral Small 3.2 |

Each entry shows its publisher, licence, parameters (total and active for mixture-of-experts models), maximum context and a short description, plus one or more **variants**:

- **GGUF** for llama.cpp: a quantization (Q4_K_M, Q8_0, ...), the exact files including split parts, and the vision projector (`mmproj`) for vision models.
- **safetensors** for vLLM or SGLang: the original weights or an FP8 / AWQ / MXFP4 checkpoint. Only the files an engine needs are downloaded (config, tokenizer, weights) - not original checkpoints, other runtimes' formats or images.

Variants marked **Licence required** come from gated repositories: accept the licence on the model's Hugging Face page and set a **Hugging Face token** in **Admin → System settings** before deploying them. Most popular gated models also have ungated GGUF variants.

Filter by task, size, engine and **fits on lane**, or search names, publishers, tags and quantizations.

## Will it fit?

For every variant the catalog estimates the GPU memory needed to serve one request:

- **weights**: the download size (plus the vision projector),
- **KV cache** for the context: `2 × layers × KV heads × head size × context × 2 bytes` (16-bit cache; an upper bound for models with sliding-window layers),
- **overhead**: about 0.5 GB + 3 % of the weights for llama.cpp, about 1.5 GB + 5 % for vLLM / SGLang (activations, CUDA graphs).

The estimate is compared with the total memory of each lane's GPUs, as reported by `nvidia-smi`:

| Badge | Meaning |
| --- | --- |
| fits | Below 95 % of the lane's memory (90 % for vLLM / SGLang, which reserve a fixed share). |
| tight | Fits only just: use a shorter context or offload fewer layers. |
| too large | Needs more memory than the lane has. llama.cpp can still run it partly on the CPU, slowly. |
| unknown | No GPU information for the lane (no NVIDIA driver, or the lane's GPUs are not detected). |

Cards show, per lane, the largest variant that fits. Change the context in the deploy dialog to see how the KV cache grows; parallel requests (`--parallel` / `-np`) need more.

## Deploying

Press **Deploy** on a card or on a variant in **Details**, then choose:

- the **variant** and, for safetensors, the **engine** (vLLM or SGLang),
- the **context length** (default 8K tokens, less when the model supports less),
- the **lane** (used for the fit check and, for vLLM / SGLang on lanes with several GPUs, tensor parallelism),
- optionally a **model name** (default `<entry>-<variant>`, for example `qwen3-8b-q4_k_m`),
- (admins) **make it the default model** on that lane.

SlotServe then queues the files in the normal download queue - so the configured Hugging Face endpoint and token are used, downloads resume after interruptions and appear under **Downloads** - after checking that the disk has room for all of them plus **Keep free after downloads**. GGUF files go into the models folder as with the Hugging Face tab; safetensors repositories go into a folder named `<org>--<repo>`. The catalog shows the progress of each deployment, and when the last file arrives the model is registered with default launch arguments:

| Engine | Arguments |
| --- | --- |
| llama.cpp | `-ngl 99 -c <context>`, plus `--mmproj <file>` for vision models and `--embedding --pooling ...` for embedding models |
| vLLM | `--max-model-len <context>`, plus `--tensor-parallel-size <GPUs>` on multi-GPU lanes and `--runner pooling` for embedding models |
| SGLang | `--context-length <context>`, plus `--tp-size <GPUs>` on multi-GPU lanes and `--is-embedding` for embedding models |

Some entries add more (for example `--jinja` for gpt-oss and the Mistral tokenizer options). Everything can be edited afterwards like any other model, and presets work as usual. Arguments for engine flags change between engine versions; if a model fails to start, check **Server log**.

Deploying something that is already there does not download again: if the files are on disk the model is registered right away, and if it is registered already it is simply reused (and made the lane default if asked). If a download fails, retry it in **Downloads**; the deployment continues when every file has arrived. Removing a deployment from the catalog's list cancels its queued downloads.

## Managing the catalog (admins)

- **Hide** a built-in entry in its **Details** to remove it from everyone's catalog; **Show hidden** brings it back.
- **Add entry** creates your own entry: name, tasks, parameters, context, descriptions, and variants given as a Hugging Face repository plus (for GGUF) a file name - the first part of split files is enough. SlotServe looks up the file sizes and split parts on Hugging Face, and reads `config.json` of the original repository for the KV-cache estimate. Your entries can be edited and deleted; deleting one keeps installed models.

The built-in list lives in `backend/slotserve/resources/model_catalog.json`. To refresh it after editing (or to check it is still accurate), run from the repository root:

```bash
PYTHONPATH=backend python tools/update_model_catalog.py          # verify and rewrite sizes
PYTHONPATH=backend python tools/update_model_catalog.py --check  # report only
```

It fails on any repository or file that no longer exists. Set `HF_TOKEN` to read gated repositories and `HF_ENDPOINT` to use a mirror.

## API

| Method | Path | |
| --- | --- | --- |
| GET | `/api/catalog?ctx=` | Entries with estimates and fit per lane (`ctx` overrides the context of every estimate) |
| GET | `/api/catalog/{entry}?ctx=` | One entry |
| POST | `/api/catalog/deploy` | `{entry_id, variant_id, engine?, lane_id?, set_default?, ctx?, name?}` |
| GET / DELETE | `/api/catalog/deploys`, `/api/catalog/deploys/{id}` | Recent deployments with progress; remove (cancel) one |
| PUT | `/api/catalog/{entry}/hidden` | Admin: `{hidden}` |
| POST / PUT / DELETE | `/api/catalog/entries`, `/api/catalog/entries/{entry}` | Admin: own entries |
