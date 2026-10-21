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
        self.execs: dict[str, dict] = {}
        self._next = 1

        def ref(name: str, tag: str) -> str:
            return f"{name}:{tag}"

        @self.app.get("/v1.41/version")
        async def version():
            return {"Version": "27.0-fake", "ApiVersion": "1.41"}

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
            eid = f"e{self._next:04d}"
            self._next += 1
            self.execs[eid] = {"Container": c["Id"], "Config": await request.json(), "Started": False}
            return JSONResponse({"Id": eid}, status_code=201)

        @self.app.post("/v1.41/exec/{eid}/start")
        async def exec_start(eid: str, request: Request):
            if eid not in self.execs:
                return JSONResponse({"message": "no such exec"}, status_code=404)
            self.execs[eid]["Started"] = (await request.json()).get("Detach")
            return Response(status_code=200)

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
            self.archives[c["Id"]] = files
            c["Modes"] = modes
            return Response(status_code=200)

        @self.app.post("/v1.41/containers/{cid}/start")
        async def start(cid: str):
            c = find(cid)
            if c is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            port = c["Config"]["HostConfig"].get("PortBindings", {}).get("22/tcp", [{}])[0].get("HostPort")
            if port in self.busy_ports:
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
        async def inspect(cid: str):
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
            return {
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
            return Response(content=b"\x01\x00\x00\x00" + len(text).to_bytes(4, "big") + text)

        @self.app.post("/v1.41/commit")
        async def commit(container: str, repo: str, tag: str):
            if find(container) is None:
                return JSONResponse({"message": "no such container"}, status_code=404)
            self.images.add(f"{repo}:{tag}")
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
