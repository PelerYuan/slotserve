"""In-process stand-in for the parts of the Docker Engine API that SlotServe uses."""

import io
import json
import tarfile
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


class FakeDocker:
    def __init__(self):
        self.app = FastAPI()
        self.images: set[str] = set()
        self.containers: dict[str, dict] = {}
        self.archives: dict[str, dict[str, bytes]] = {}
        self.busy_ports: set[str] = set()
        self.pulls: list[str] = []
        self.networks: dict[str, dict] = {}
        self.execs: dict[str, dict] = {}  # exec id -> {Container, Config, Started}
        self.exec_calls: list[tuple[str, list[str]]] = []  # (container id, command) of every exec
        self.exec_result: tuple[int, bytes] = (0, b"")
        self.listening: bytes | None = None  # /proc/net/tcp contents the port probe sees (None: exec_result)
        self.wait_code: int | None = None  # exit code reported for helper containers (default: their own)
        self.removed_volumes: list[str] = []
        self.commits: list[tuple[str, str]] = []
        self.loads: list[tuple[str, int]] = []  # (image, bytes) of every docker load
        self.created: list[tuple[str, dict]] = []  # (name, config) of every container ever created
        # Storage (services/storage.py): named volumes, what helper containers print, mounts that fail
        self.volumes: dict[str, dict] = {}  # name -> {Name, Driver, Options, Labels}
        self.helper_output: dict[str, str] = {}  # text in a helper's script -> its output (logs)
        self.mount_errors: dict[str, str] = {}  # mount source (prefix) -> error when a container starts
        self._exec_ids: dict[str, int] = {}
        self.ncpu = 32  # what /info reports
        self.mem_total = 128 * 2**30
        self.cpu_ns: dict[str, int] = {}  # container id -> CPU time reported by /stats
        self.mem_used: dict[str, int] = {}
        self.updates: list[tuple[str, dict]] = []  # (container id, body) of every docker update
        self._next = 1

        def ref(name: str, tag: str) -> str:
            return f"{name}:{tag}"

        @self.app.get("/v1.41/version")
        async def version():
            return {"Version": "27.0-fake", "ApiVersion": "1.41"}

        @self.app.get("/v1.41/info")
        async def info():
            return {"NCPU": self.ncpu, "MemTotal": self.mem_total, "ServerVersion": "27.0-fake"}

        @self.app.get("/v1.41/containers/{cid}/stats")
        async def stats(cid: str):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            host = c["Config"].get("HostConfig", {})
            return {
                "cpu_stats": {"cpu_usage": {"total_usage": self.cpu_ns.get(c["Id"], 0)}, "online_cpus": self.ncpu},
                "memory_stats": {
                    "usage": self.mem_used.get(c["Id"], 0) + 4096,
                    "limit": host.get("Memory") or self.mem_total,
                    "stats": {"inactive_file": 4096},
                },
            }

        @self.app.post("/v1.41/containers/{cid}/update")
        async def update(cid: str, request: Request):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            body = await request.json()
            self.updates.append((c["Id"], body))
            c["Config"].setdefault("HostConfig", {}).update(body)
            return {"Warnings": []}

        @self.app.get("/v1.41/images/{name:path}/json")
        async def image(name: str):
            if unquote(name) in self.images:
                return {"Id": "sha256:" + unquote(name)}
            return JSONResponse({"message": f"No such image: {unquote(name)}"}, status_code=404)

        @self.app.post("/v1.41/images/create")
        async def pull(fromImage: str, tag: str = "latest"):
            full = ref(fromImage, tag)
            self.pulls.append(full)

            async def events():
                if "missing" in full:
                    yield json.dumps({"error": f"manifest for {full} not found"}) + "\n"
                    return
                for layer in ("a", "b"):
                    for current in (50, 100):
                        detail = {"current": current, "total": 100}
                        yield json.dumps({"id": layer, "status": "Downloading", "progressDetail": detail}) + "\n"
                    yield json.dumps({"id": layer, "status": "Pull complete", "progressDetail": {}}) + "\n"
                self.images.add(full)
                yield json.dumps({"status": f"Status: Downloaded newer image for {full}"}) + "\n"

            return StreamingResponse(events(), media_type="application/json")

        @self.app.get("/v1.41/images/{name:path}/get")
        async def image_save(name: str):
            # ``docker save``: a stand-in archive naming the image, large enough to need several chunks
            if unquote(name) not in self.images:
                return JSONResponse({"message": f"No such image: {unquote(name)}"}, status_code=404)
            payload = b"IMAGE:" + unquote(name).encode() + b"\n" + b"\0" * 300_000
            return Response(content=payload, media_type="application/x-tar")

        @self.app.post("/v1.41/images/load")
        async def image_load(request: Request):
            body = await request.body()
            name = body.split(b"\n", 1)[0].removeprefix(b"IMAGE:").decode()
            self.images.add(name)
            self.loads.append((name, len(body)))
            return {"stream": f"Loaded image: {name}\n"}

        @self.app.delete("/v1.41/images/{name:path}")
        async def remove_image(name: str):
            self.images.discard(unquote(name))
            return [{"Untagged": unquote(name)}]

        @self.app.post("/v1.41/containers/create")
        async def create(name: str, request: Request):
            config = await request.json()
            if any(c["Name"] == name for c in self.containers.values()):
                return JSONResponse({"message": f"Conflict. The container name {name} is in use"}, status_code=409)
            if config["Image"] not in self.images:
                return JSONResponse({"message": f"No such image: {config['Image']}"}, status_code=404)
            mode = config.get("HostConfig", {}).get("NetworkMode")
            if mode and mode not in ("default", "bridge", "host", "none") and net(mode) is None:
                return JSONResponse({"message": f"network {mode} not found"}, status_code=404)
            cid = f"c{self._next:04d}"
            self._next += 1
            self.created.append((name, config))
            self.containers[cid] = {"Id": cid, "Name": name, "Config": config, "Running": False, "ExitCode": 0}
            if mode and net(mode) is not None:
                net(mode)["Containers"].add(cid)
            return JSONResponse({"Id": cid}, status_code=201)

        def net(ref: str) -> dict | None:
            return self.networks.get(ref) or next((n for n in self.networks.values() if n["Name"] == ref), None)

        @self.app.post("/v1.41/networks/create")
        async def network_create(request: Request):
            body = await request.json()
            if net(body["Name"]) is not None:
                return JSONResponse({"message": f"network with name {body['Name']} already exists"}, status_code=409)
            nid = f"n{self._next:04d}"
            self._next += 1
            self.networks[nid] = {
                "Id": nid,
                "Name": body["Name"],
                "Labels": body.get("Labels") or {},
                "Containers": set(),
            }
            return JSONResponse({"Id": nid}, status_code=201)

        @self.app.post("/v1.41/networks/{ref}/connect")
        async def network_connect(ref: str, request: Request):
            n = net(unquote(ref))
            if n is None:
                return JSONResponse({"message": "network not found"}, status_code=404)
            container = (await request.json())["Container"]
            if container in n["Containers"]:
                return JSONResponse({"message": f"endpoint with name {container} already exists"}, status_code=403)
            n["Containers"].add(container)
            return Response(status_code=200)

        @self.app.post("/v1.41/networks/{ref}/disconnect")
        async def network_disconnect(ref: str, request: Request):
            n = net(unquote(ref))
            if n is None:
                return JSONResponse({"message": "network not found"}, status_code=404)
            n["Containers"].discard((await request.json())["Container"])
            return Response(status_code=200)

        @self.app.delete("/v1.41/networks/{ref}")
        async def network_remove(ref: str):
            n = net(unquote(ref))
            if n is None:
                return JSONResponse({"message": "network not found"}, status_code=404)
            if n["Containers"]:
                return JSONResponse({"message": "error while removing network: has active endpoints"}, status_code=403)
            del self.networks[n["Id"]]
            return Response(status_code=204)

        @self.app.get("/v1.41/networks")
        async def network_list(filters: str = "{}"):
            wanted = json.loads(filters).get("label", [])
            return [
                {"Id": n["Id"], "Name": n["Name"], "Labels": n["Labels"]}
                for n in self.networks.values()
                if all(n["Labels"].get(k) == v for k, _, v in (w.partition("=") for w in wanted))
            ]

        @self.app.post("/v1.41/containers/{cid}/exec")
        async def exec_create(cid: str, request: Request):
            c = find(cid)
            if c is None or not c["Running"]:
                return JSONResponse({"message": f"container {cid} is not running"}, status_code=409)
            body = await request.json()
            eid = f"e{self._next:04d}"
            self._next += 1
            self.execs[eid] = {"Container": c["Id"], "Config": body, "Started": False}
            self.exec_calls.append((c["Id"], body["Cmd"]))
            return JSONResponse({"Id": eid}, status_code=201)

        @self.app.post("/v1.41/exec/{eid}/start")
        async def exec_start(eid: str, request: Request):
            if eid not in self.execs:
                return JSONResponse({"message": "no such exec"}, status_code=404)
            detach = bool((await request.json()).get("Detach"))
            self.execs[eid]["Started"] = detach
            if detach:
                return Response(status_code=200)
            code, text = self.exec_result
            if self.listening is not None and "proc/net/tcp" in " ".join(self.execs[eid]["Config"]["Cmd"]):
                code, text = 0, self.listening  # what the port probe reads (services/devenv_ports.py)
            self._exec_ids[eid] = code
            return Response(content=b"\x01\x00\x00\x00" + len(text).to_bytes(4, "big") + text if text else b"")

        @self.app.get("/v1.41/exec/{eid}/json")
        async def exec_inspect(eid: str):
            return {"ExitCode": self._exec_ids.get(eid, 0), "Running": False}

        def find(cid: str) -> dict | None:
            return self.containers.get(cid) or next((c for c in self.containers.values() if c["Name"] == cid), None)

        @self.app.put("/v1.41/containers/{cid}/archive")
        async def archive(cid: str, path: str, request: Request):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            with tarfile.open(fileobj=io.BytesIO(await request.body())) as tar:
                files = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
                modes = {m.name: m.mode for m in tar.getmembers()}
            self.archives.setdefault(c["Id"], {}).update(files)
            c.setdefault("Modes", {}).update(modes)
            return Response(status_code=200)

        @self.app.post("/v1.41/containers/{cid}/start")
        async def start(cid: str):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            for m in c["Config"].get("HostConfig", {}).get("Mounts") or []:
                for prefix, error in self.mount_errors.items():
                    if str(m.get("Source", "")).startswith(prefix):
                        return JSONResponse({"message": error}, status_code=500)
                if m.get("Type") == "volume" and m["Source"] not in self.volumes:
                    self.volumes[m["Source"]] = {"Name": m["Source"], "Driver": "local", "Options": {}, "Labels": {}}
            # any published host port (SSH, forwarders of direct ports) can be taken by something else
            for bindings in c["Config"]["HostConfig"].get("PortBindings", {}).values():
                port = (bindings or [{}])[0].get("HostPort")
                if port and port in self.busy_ports:
                    message = f"Bind for 0.0.0.0:{port} failed: port is already allocated"
                    return JSONResponse({"message": message}, status_code=500)
            c["Running"] = True
            return Response(status_code=204)

        @self.app.post("/v1.41/containers/{cid}/stop")
        async def stop(cid: str):
            c = find(cid)
            if c is None:
                return Response(status_code=404)
            c["Running"] = False
            return Response(status_code=204)

        @self.app.delete("/v1.41/containers/{cid}")
        async def remove(cid: str):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            del self.containers[c["Id"]]
            for n in self.networks.values():
                n["Containers"].discard(c["Id"])
            return Response(status_code=204)

        @self.app.get("/v1.41/containers/{cid}/json")
        async def inspect(cid: str, size: str = "false"):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            ports = {}
            for spec, bindings in c["Config"].get("HostConfig", {}).get("PortBindings", {}).items():
                c.setdefault("Assigned", {}).setdefault(spec, str(32768 + len(c["Assigned"])))
                ports[spec] = [
                    {"HostIp": b.get("HostIp", ""), "HostPort": b.get("HostPort") or c["Assigned"][spec]}
                    for b in bindings
                ]
            out = {
                "Id": c["Id"],
                "Name": "/" + c["Name"],
                "State": {"Running": c["Running"], "ExitCode": c["ExitCode"]},
                "NetworkSettings": {
                    "Ports": ports if c["Running"] else {},
                    "Networks": {
                        n["Name"]: {"IPAddress": f"172.30.{i}.2"}
                        for i, n in enumerate(self.networks.values())
                        if c["Id"] in n["Containers"]
                    },
                },
            }
            if size == "true":
                out["SizeRw"] = 4096
            return out

        @self.app.post("/v1.41/containers/{cid}/wait")
        async def wait(cid: str):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            c["Running"] = False
            return {"StatusCode": self.wait_code if self.wait_code is not None else c["ExitCode"]}

        @self.app.delete("/v1.41/volumes/{name}")
        async def remove_volume(name: str):
            name = unquote(name)
            in_use = any(
                m.get("Source") == name
                for c in self.containers.values()
                for m in c["Config"].get("HostConfig", {}).get("Mounts") or []
            )
            if in_use:
                return JSONResponse({"message": f"remove {name}: volume is in use"}, status_code=409)
            self.removed_volumes.append(name)
            self.volumes.pop(name, None)
            return Response(status_code=204)

        @self.app.post("/v1.41/volumes/create")
        async def volume_create(request: Request):
            body = await request.json()
            name = body["Name"]
            if name not in self.volumes:
                self.volumes[name] = {
                    "Name": name,
                    "Driver": body.get("Driver") or "local",
                    "Options": body.get("DriverOpts") or {},
                    "Labels": body.get("Labels") or {},
                }
            return JSONResponse(self.volumes[name], status_code=201)

        @self.app.get("/v1.41/volumes/{name}")
        async def volume_inspect(name: str):
            v = self.volumes.get(unquote(name))
            if v is None:
                return JSONResponse({"message": "no such volume"}, status_code=404)
            return v

        @self.app.get("/v1.41/volumes")
        async def volume_list(filters: str = "{}"):
            wanted = [w.partition("=") for w in json.loads(filters).get("label", [])]

            def match(labels: dict) -> bool:
                return all((labels.get(k) == v) if sep else (k in labels) for k, sep, v in wanted)

            return {"Volumes": [v for v in self.volumes.values() if match(v["Labels"])], "Warnings": []}

        @self.app.get("/v1.41/containers/json")
        async def listing(filters: str = "{}"):
            wanted = json.loads(filters).get("label", [])
            out = []
            for c in self.containers.values():
                labels = c["Config"].get("Labels", {})
                if all(labels.get(k) == v for k, _, v in (w.partition("=") for w in wanted)):
                    out.append({"Id": c["Id"], "Labels": labels, "State": "running" if c["Running"] else "exited"})
            return out

        @self.app.get("/v1.41/containers/{cid}/logs")
        async def logs(cid: str):
            c = find(cid)
            text = b"dropbear: Not backgrounding\n" + (b"Killed\n" if c and c["ExitCode"] else b"")
            script = " ".join((c or {}).get("Config", {}).get("Entrypoint") or [])
            for needle, output in self.helper_output.items():
                if needle in script:
                    text = output.encode()
                    break
            return Response(content=b"\x01\x00\x00\x00" + len(text).to_bytes(4, "big") + text)

        @self.app.post("/v1.41/commit")
        async def commit(container: str, repo: str, tag: str):
            if find(container) is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            self.images.add(f"{repo}:{tag}")
            self.commits.append((find(container)["Id"], f"{repo}:{tag}"))
            return JSONResponse({"Id": "sha256:saved"}, status_code=201)

    def add_container(self, name: str, labels: dict, running: bool = True) -> str:
        cid = f"c{self._next:04d}"
        self._next += 1
        self.containers[cid] = {
            "Id": cid,
            "Name": name,
            "Config": {"Labels": labels, "HostConfig": {}},
            "Running": running,
            "ExitCode": 0,
        }
        return cid

    def forwarders(self) -> dict[str, dict]:
        """Forwarder containers of direct ports, by name."""
        return {
            c["Name"]: c
            for c in self.containers.values()
            if c["Config"].get("Labels", {}).get("slotserve.devenv") == "forwarder"
        }
