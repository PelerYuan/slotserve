# 常见问题

**模型加载要好几分钟，能更快吗？**
加载速度取决于磁盘。把常用模型放到 SSD 上（让 `MODELS_DIR` 指向 SSD，或对单个文件做软链接）。Linux 会用空闲内存缓存最近读过的文件，内存大于模型时第二次加载会快很多。SlotServe 会在预约前预加载，尽量把等待藏起来。

**客户端收到 `503 model_unavailable`。**
模型正在加载或启动失败。`Retry-After` 头给出了大致的等待时间。如果一直失败，打开**模型 → 运行日志**查看 llama-server 的报错（常见原因是 `-c` 太大导致显存不足，或参数写错）。

**客户端收到 `409 model_not_loaded`。**
请求里写了另一个已登记的模型名。去掉 `model` 或改成当前模型，或者预约你想用的模型。

**多个人能同时用 GPU 吗？**
可以，在没人预约时：所有有效密钥都能并发使用默认模型（在启动参数里调大 `-np` 可增加并发 slot）。预约期间 GPU 是独占的。

**支持多张 GPU 或同时加载多个模型吗？**
暂不支持。SlotServe 管理一个 llama-server 进程；你仍可以给这个进程传 llama.cpp 的多卡参数（如 `--split-mode`、`--tensor-split`）。

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

**llama-server 的日志在哪？**
`DATA_DIR/logs/llama-server.log`（超过 20 MB 会轮转），网页上也能查看。
