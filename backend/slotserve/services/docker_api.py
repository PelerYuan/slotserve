"""A small client for the Docker Engine API (Podman's Docker-compatible API works too)."""

import io
import json
import logging
import tarfile
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API = "/v1.41"


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def split_image(ref: str) -> tuple[str, str]:
    """``registry:5000/repo/name:tag`` → (``registry:5000/repo/name``, ``tag``); no tag means ``latest``."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, digest
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :]
    return ref, "latest"


def registry_of(ref: str) -> str:
    """The registry host an image comes from (``docker.io`` when none is named)."""
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return "docker.io"


def tar_files(files: dict[str, tuple[bytes, int]]) -> bytes:
    """A tar archive of ``{path: (content, mode)}``; directories are created owned by root."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dirs: set[str] = set()
        for path in files:
            parts = path.strip("/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for d in sorted(dirs):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700 if d.endswith("ssh") else 0o755
            info.uid = info.gid = 0
            tar.addfile(info)
        for path, (content, mode) in files.items():
            info = tarfile.TarInfo(path.strip("/"))
            info.size = len(content)
            info.mode = mode
            info.uid = info.gid = 0
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class DockerClient:
    def __init__(self, host: str, transport: httpx.AsyncBaseTransport | None = None):
        self.host = host
        base = "http://docker"
        if host.startswith("tcp://"):
            base = host.replace("tcp://", "http://", 1)
        elif transport is None and host.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=host[len("unix://") :])
        self._http = httpx.AsyncClient(
            base_url=base + API, transport=transport, timeout=httpx.Timeout(60.0, connect=5.0)
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 204), **kw) -> httpx.Response:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code not in ok:
            try:
                message = resp.json().get("message") or resp.text
            except ValueError:
                message = resp.text
            raise DockerError(resp.status_code, message.strip() or f"HTTP {resp.status_code}")
        return resp

    # ---- system -------------------------------------------------------------

    async def version(self) -> dict:
        return (await self._request("GET", "/version")).json()

    async def info(self) -> dict:
        return (await self._request("GET", "/info")).json()

    # ---- images -------------------------------------------------------------

    async def image_exists(self, ref: str) -> bool:
        try:
            await self._request("GET", f"/images/{quote(ref, safe='')}/json")
            return True
        except DockerError as exc:
            if exc.status == 404:
                return False
            raise

    async def pull(self, ref: str, progress: Callable[[float | None, str], Awaitable[None]] | None = None) -> None:
        """Pulls an image, reporting (percent, status) as layers download."""
        repo, tag = split_image(ref)
        layers: dict[str, tuple[int, int]] = {}
        params = {"fromImage": repo, "tag": tag}
        async with self._http.stream("POST", "/images/create", params=params, timeout=None) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                try:
                    body = json.loads(body).get("message", body)
                except ValueError:
                    pass
                raise DockerError(resp.status_code, body.strip())
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error"):
                    raise DockerError(500, event["error"])
                detail = event.get("progressDetail") or {}
                if event.get("id") and detail.get("total"):
                    layers[event["id"]] = (int(detail.get("current", 0)), int(detail["total"]))
                elif event.get("id") and event.get("status") in ("Pull complete", "Already exists"):
                    done = layers.get(event["id"], (1, 1))[1]
                    layers[event["id"]] = (done, done)
                if progress is not None:
                    total = sum(t for _, t in layers.values())
                    pct = 100.0 * sum(c for c, _ in layers.values()) / total if total else None
                    await progress(pct, event.get("status", ""))

    async def remove_image(self, ref: str) -> None:
        await self._request("DELETE", f"/images/{quote(ref, safe='')}", ok=(200, 404))

    async def commit(self, container: str, ref: str, comment: str = "") -> str:
        repo, tag = split_image(ref)
        params = {"container": container, "repo": repo, "tag": tag, "comment": comment, "pause": "true"}
        resp = await self._request("POST", "/commit", params=params, timeout=None)
        return resp.json().get("Id", "")

    # ---- containers -----------------------------------------------------------

    async def create(self, name: str, config: dict) -> str:
        resp = await self._request("POST", "/containers/create", params={"name": name}, json=config)
        return resp.json()["Id"]

    async def put_archive(self, container: str, path: str, data: bytes) -> None:
        await self._request(
            "PUT",
            f"/containers/{container}/archive",
            params={"path": path},
            content=data,
            headers={"Content-Type": "application/x-tar"},
        )

    async def start(self, container: str) -> None:
        await self._request("POST", f"/containers/{container}/start", ok=(204, 304))

    async def stop(self, container: str, timeout: int = 10) -> None:
        await self._request(
            "POST", f"/containers/{container}/stop", params={"t": timeout}, ok=(204, 304, 404), timeout=timeout + 30
        )

    async def remove(self, container: str) -> None:
        await self._request("DELETE", f"/containers/{container}", params={"force": "true"}, ok=(204, 404))

    async def inspect(self, container: str, size: bool = False) -> dict | None:
        params = {"size": "true"} if size else None
        try:
            return (await self._request("GET", f"/containers/{container}/json", params=params)).json()
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise

    async def stats(self, container: str) -> dict | None:
        """One reading of a container's resource use (CPU time in ns, memory); None when it is gone."""
        try:
            resp = await self._request(
                "GET", f"/containers/{container}/stats", params={"stream": "false", "one-shot": "true"}
            )
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.json()

    async def update(self, container: str, resources: dict) -> None:
        """Changes a container's CPU and memory limits (``docker update``)."""
        await self._request("POST", f"/containers/{container}/update", json=resources)

    async def wait(self, container: str, timeout: float = 600) -> int:
        """Waits for a container to exit; returns its exit code."""
        resp = await self._request("POST", f"/containers/{container}/wait", timeout=timeout)
        return int(resp.json().get("StatusCode", 0))

    async def exec(self, container: str, cmd: list[str], timeout: float = 120) -> tuple[int, str]:
        """Runs a command as root in a running container; returns (exit code, output)."""
        created = await self._request(
            "POST",
            f"/containers/{container}/exec",
            json={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True, "User": "root"},
        )
        exec_id = created.json()["Id"]
        start = {"Detach": False, "Tty": False}
        out = await self._request("POST", f"/exec/{exec_id}/start", json=start, timeout=timeout)
        info = (await self._request("GET", f"/exec/{exec_id}/json")).json()
        code = info.get("ExitCode")
        return (int(code) if code is not None else -1), _demux(out.content)

    async def remove_volume(self, name: str) -> None:
        await self._request("DELETE", f"/volumes/{quote(name, safe='')}", ok=(204, 404))

    async def list(self, label: str) -> list[dict[str, Any]]:
        filters = json.dumps({"label": [label]})
        return (await self._request("GET", "/containers/json", params={"all": "true", "filters": filters})).json()

    async def exec_detached(self, container: str, cmd: Sequence[str], user: str = "root") -> str:
        """Runs a command inside a running container in the background (``docker exec -d``)."""
        body = {"Cmd": [*cmd], "User": user, "AttachStdin": False, "AttachStdout": False, "AttachStderr": False}
        resp = await self._request("POST", f"/containers/{container}/exec", json=body)
        exec_id = resp.json()["Id"]
        await self._request("POST", f"/exec/{exec_id}/start", json={"Detach": True, "Tty": False}, ok=(200, 204))
        return exec_id

    # ---- networks -------------------------------------------------------------

    async def network_create(self, name: str, labels: dict[str, str]) -> str:
        body = {"Name": name, "Driver": "bridge", "CheckDuplicate": True, "Labels": labels}
        return (await self._request("POST", "/networks/create", json=body)).json()["Id"]

    async def network_connect(self, network: str, container: str, aliases: Sequence[str] | None = None) -> None:
        body: dict[str, Any] = {"Container": container}
        if aliases:
            body["EndpointConfig"] = {"Aliases": [*aliases]}
        try:
            await self._request("POST", f"/networks/{quote(network, safe='')}/connect", json=body)
        except DockerError as exc:
            if "already exists" in exc.message or "already attached" in exc.message:
                return
            raise

    async def network_disconnect(self, network: str, container: str) -> None:
        body = {"Container": container, "Force": True}
        await self._request("POST", f"/networks/{quote(network, safe='')}/disconnect", json=body, ok=(200, 404))

    async def network_remove(self, network: str) -> None:
        await self._request("DELETE", f"/networks/{quote(network, safe='')}", ok=(200, 204, 404))

    async def networks(self, label: str) -> "list[dict[str, Any]]":
        filters = json.dumps({"label": [label]})
        return (await self._request("GET", "/networks", params={"filters": filters})).json()

    async def logs(self, container: str, tail: int = 200) -> str:
        resp = await self._request(
            "GET",
            f"/containers/{container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail), "timestamps": "false"},
            ok=(200, 404),
        )
        return _demux(resp.content)


def _demux(raw: bytes) -> str:
    """Logs of a container without a TTY come in 8-byte-header frames; with a TTY they are plain."""
    out = bytearray()
    i = 0
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4 : i + 8], "big")
            out += raw[i + 8 : i + 8 + size]
            i += 8 + size
        return out.decode(errors="replace")
    return raw.decode(errors="replace")
