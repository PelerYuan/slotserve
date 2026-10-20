# 常见问题

**模型加载要好几分钟，能更快吗？**
加载速度取决于磁盘。把常用模型放到 SSD 上（让 `MODELS_DIR` 指向 SSD，或对单个文件做软链接）。Linux 会用空闲内存缓存最近读过的文件，内存大于模型时第二次加载会快很多。SlotServe 会在预约前预加载，尽量把等待藏起来。

**客户端收到 `503 model_unavailable`。**
模型正在加载或启动失败。`Retry-After` 头给出了大致的等待时间。如果一直失败，打开**模型 → 运行日志**查看推理进程的报错（常见原因是 `-c` 太大导致显存不足，或参数写错）。

**客户端收到 `409 model_not_loaded`。**
请求里写了另一个已登记的模型名。去掉 `model` 或改成当前模型，或者预约你想用的模型。

**多个人能同时用 GPU 吗？**
可以，在没人预约时：所有有效密钥都能并发使用默认模型（在启动参数里调大 `-np` 可增加并发 slot）。预约期间 GPU 是独占的。

**支持多张 GPU 或同时加载多个模型吗？**
支持。在**管理 → GPU 与通道**中把 GPU 分成多个通道，每个通道运行自己的模型、单独预约，因此可以同时加载多个模型。要让一个模型用多张卡，把这些卡放进同一个通道，并在参数里加上对应后端的多卡参数（llama.cpp 的 `--split-mode`/`--tensor-split`，vLLM 的 `--tensor-parallel-size`）。详见[多 GPU 与推理后端](gpus-and-engines.md)。

**能用 vLLM 或 SGLang 代替 llama.cpp 吗？**
可以，按模型选择：**模型 → 添加模型**，选推理后端即可。后端需要安装在 SlotServe 运行的环境里，`Dockerfile.vllm` 可以构建带 vLLM 的镜像。

**忘记密码了。**
管理员可以执行 `docker compose exec slotserve slotserve reset-password <用户名>`。

**如何放在 HTTPS / 反向代理后面？**
把所有请求转发到 8000 端口，并关闭缓冲以支持流式输出。nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

然后设置 `SLOTSERVE_COOKIE_SECURE=true`。

**如何备份？**
复制 `DATA_DIR`（里面有 `slotserve.db`）。模型在 `MODELS_DIR` 中，除了下载和显式删除外不会被改动。

**推理进程的日志在哪？**
第一个通道是 `DATA_DIR/logs/llama-server.log`，其他通道是 `llama-server-lane<N>.log`（超过 20 MB 会轮转），网页上也能查看。各种后端的日志文件名相同。
