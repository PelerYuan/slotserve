"""Verifies the built-in model catalog against the Hugging Face API and records exact sizes.

    PYTHONPATH=backend python tools/update_model_catalog.py          # check and rewrite sizes in place
    PYTHONPATH=backend python tools/update_model_catalog.py --check  # only report problems (exit 1 on any)

For every variant it lists the repository (``/api/models/<repo>/tree/<rev>``), fails when a listed
GGUF file is missing, and stores file sizes; safetensors variants get the total size and file count
of what a deploy would download. It also records whether a repository is gated, and reads each
model's ``config.json`` (``config_repo`` or ``source_repo``) for the layer / KV-head numbers the
memory estimate uses. Set HF_TOKEN to read gated repositories; HF_ENDPOINT for a mirror.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

from slotserve.services import catalog

ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
TOKEN = os.environ.get("HF_TOKEN", "")
_cache: dict[str, object] = {}


def fetch(path: str):
    if path in _cache:
        return _cache[path]
    req = urllib.request.Request(f"{ENDPOINT}{path}")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                data = None
                break
            if attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
    _cache[path] = data
    return data


def tree(repo: str, revision: str) -> dict[str, int]:
    items = fetch(f"/api/models/{repo}/tree/{revision}?recursive=true")
    if items is None:
        raise LookupError(f"repository {repo} not found")
    files = (i for i in items if i["type"] == "file")
    return {i["path"]: int((i.get("lfs") or {}).get("size") or i.get("size") or 0) for i in files}


def arch_from_config(cfg: dict) -> dict | None:
    text = cfg.get("text_config") or cfg.get("llm_config") or cfg
    layers = text.get("num_hidden_layers")
    heads = text.get("num_attention_heads")
    if not layers or not heads:
        return None
    kv = text.get("num_key_value_heads") or heads
    head_dim = text.get("head_dim") or text["hidden_size"] // heads
    return {"layers": layers, "kv_heads": kv, "head_dim": head_dim}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report only, do not write")
    parser.add_argument("--file", default=str(catalog.CATALOG_FILE))
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as f:
        raw = json.load(f)
    before = json.dumps(raw["entries"], sort_keys=True)
    problems: list[str] = []
    for entry in raw["entries"]:
        for v in entry["variants"]:
            label = f"{entry['id']}/{v['id']}"
            try:
                files = tree(v["repo"], v.get("revision", "main"))
            except LookupError as exc:
                problems.append(f"{label}: {exc}")
                continue
            info = fetch(f"/api/models/{v['repo']}") or {}
            v["gated"] = bool(info.get("gated"))
            if v["format"] == "gguf":
                missing = [p for p in v["files"] + ([v["mmproj"]] if v.get("mmproj") else []) if p not in files]
                if missing:
                    problems.append(f"{label}: missing {missing}")
                    continue
                size = sum(files[p] for p in v["files"])
                v["size_bytes"] = size
                if v.get("mmproj"):
                    v["mmproj_size_bytes"] = files[v["mmproj"]]
            else:
                model = catalog.Variant.model_validate(v)
                chosen = catalog.select_files([{"type": "file", "path": p, "size": s} for p, s in files.items()], model)
                if not any(p.endswith(".safetensors") for p, _ in chosen) or "config.json" not in dict(chosen):
                    problems.append(f"{label}: no safetensors weights / config.json selected")
                    continue
                v["size_bytes"] = sum(s for _, s in chosen)
                v["file_count"] = len(chosen)
            print(f"ok  {label:58} {v['size_bytes'] / 1e9:8.2f} GB{'  gated' if v['gated'] else ''}")
        cfg_repo = entry.get("config_repo") or entry.get("source_repo")
        if cfg_repo and entry.get("arch", "auto") is not None:
            cfg = fetch(f"/{cfg_repo}/resolve/main/config.json")
            arch = arch_from_config(cfg) if isinstance(cfg, dict) else None
            if arch:
                entry["arch"] = arch
            elif not entry.get("arch"):
                problems.append(f"{entry['id']}: could not read config.json from {cfg_repo}")
    try:
        catalog.CatalogFile.model_validate(raw)
    except ValueError as exc:
        problems.append(f"validation: {exc}")
    if args.check and json.dumps(raw["entries"], sort_keys=True) != before:
        problems.append("sizes, gated flags or architecture differ from Hugging Face (run without --check)")
    for p in problems:
        print("ERR", p, file=sys.stderr)
    if problems:
        return 1
    if not args.check:
        raw["updated"] = datetime.date.today().isoformat()
        with open(args.file, "w", encoding="utf-8", newline="\n") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"wrote {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
