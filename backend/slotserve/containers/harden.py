"""harden(): the last step before a container body reaches Docker. Not a contributor - nothing runs after it.

It is a pure function, idempotent, and only ever makes a body stricter:

* refuses ``Privileged``, ``CapAdd``, the host's network/PID/IPC/UTS/user namespaces, a bind of the host's root
  directory or of the Docker socket, and a GPU request for "all GPUs" (``Count: -1``);
* adds ``no-new-privileges`` (not for templates in compatibility mode, D14), drops the capabilities ordinary work does
  not need, limits processes (``PidsLimit``) and memory (the node's default: host memory less a reserve), raises the
  open-files limit to a usable value and bounds the container's log (``json-file``, 50 MB x 5);
* labels the body ``slotserve.hardened`` so ``check()`` - called by ``DockerClient.create`` for every container - can
  refuse anything that did not come through here.

The control plane and (later) node agents run the same code (A01, arch3-2 K1).
"""

import copy
import posixpath

from .model import Kind, NodePolicy

VERSION = "1"
HARDENED_LABEL = "slotserve.hardened"
COMPAT_LABEL = "slotserve.compat"
NO_NEW_PRIVILEGES = "no-new-privileges:true"
CAP_DROP = ("NET_RAW", "MKNOD", "SYS_CHROOT", "AUDIT_WRITE", "SETFCAP")
NOFILE = 1048576
LOG_CONFIG = {"Type": "json-file", "Config": {"max-size": "50m", "max-file": "5"}}
HOST_NAMESPACES = ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run")
COMPAT_KINDS = ("booking", "shared", "cpu", "project")  # helpers and forwarders never get compatibility mode


class HardenError(Exception):
    """A body asks for something no container of SlotServe may have (an error code for the person)."""

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def _forbidden_source(source: str) -> bool:
    if not source or not source.startswith("/"):
        return False  # a named volume
    path = posixpath.normpath(source)
    return path == "/" or path in DOCKER_SOCKETS


def _bind_source(bind: str) -> str:
    return bind.split(":", 1)[0]


def problems(body: dict) -> list[str]:
    """What makes a body unacceptable whether or not it was hardened (shared by harden() and check())."""
    host = body.get("HostConfig") or {}
    found = []
    if host.get("Privileged"):
        found.append("Privileged")
    if host.get("CapAdd"):
        found.append("CapAdd")
    for key in HOST_NAMESPACES:
        if str(host.get(key) or "") == "host":
            found.append(f"{key}=host")
    for bind in host.get("Binds") or []:
        if _forbidden_source(_bind_source(bind)):
            found.append(f"bind of {_bind_source(bind)}")
    for m in host.get("Mounts") or []:
        if m.get("Type") == "bind" and _forbidden_source(str(m.get("Source") or "")):
            found.append(f"bind of {m.get('Source')}")
    if host.get("Devices"):
        found.append("Devices")
    for request in host.get("DeviceRequests") or []:
        if request.get("Count") == -1 or (request.get("Count") is None and not request.get("DeviceIDs")):
            found.append("all GPUs")
    return found


def harden(body: dict, *, kind: Kind, node_policy: NodePolicy, compat: bool = False) -> dict:
    """Returns a hardened copy of a Docker create body (raises HardenError for what can never be allowed)."""
    found = problems(body)
    if "all GPUs" in found:
        raise HardenError("devenv_lane_no_gpus")
    if found:
        raise HardenError("container_forbidden", detail=", ".join(found))
    out = copy.deepcopy(body)
    host = out.setdefault("HostConfig", {})
    labels = out.setdefault("Labels", {})
    compat = compat and kind in COMPAT_KINDS
    security = [o for o in host.get("SecurityOpt") or [] if not o.startswith("no-new-privileges")]
    if compat:
        labels[COMPAT_LABEL] = "1"
    else:
        security.append(NO_NEW_PRIVILEGES)
        labels.pop(COMPAT_LABEL, None)
    if security:
        host["SecurityOpt"] = security
    drop = list(host.get("CapDrop") or [])
    if "ALL" not in drop:
        drop += [c for c in CAP_DROP if c not in drop]
    host["CapDrop"] = drop
    if not (isinstance(host.get("PidsLimit"), int) and host["PidsLimit"] > 0):
        host["PidsLimit"] = node_policy.pids_limit
    else:
        host["PidsLimit"] = min(host["PidsLimit"], node_policy.pids_limit)
    if not (isinstance(host.get("Memory"), int) and host["Memory"] > 0):
        memory = node_policy.default_memory()
        if memory is None:
            raise HardenError("devenv_memory_unknown")
        host["Memory"] = memory
    ulimits = [u for u in host.get("Ulimits") or [] if u.get("Name") != "nofile"]
    host["Ulimits"] = [*ulimits, {"Name": "nofile", "Soft": NOFILE, "Hard": NOFILE}]
    log = host.get("LogConfig") or {}
    if log.get("Type") != "json-file" or not (log.get("Config") or {}).get("max-size"):
        host["LogConfig"] = copy.deepcopy(LOG_CONFIG)
    labels[HARDENED_LABEL] = VERSION
    return out


def check(body: dict) -> list[str]:
    """Why a body must not be created: empty when it went through harden() and nobody loosened it since."""
    host = body.get("HostConfig") or {}
    labels = body.get("Labels") or {}
    found = problems(body)
    if labels.get(HARDENED_LABEL) != VERSION:
        found.append("not hardened")
    if labels.get(COMPAT_LABEL) != "1" and not any(
        o.startswith("no-new-privileges") for o in host.get("SecurityOpt") or []
    ):
        found.append("no-new-privileges missing")
    if not (isinstance(host.get("PidsLimit"), int) and host["PidsLimit"] > 0):
        found.append("PidsLimit missing")
    if not (isinstance(host.get("Memory"), int) and host["Memory"] > 0):
        found.append("Memory missing")
    if not host.get("LogConfig"):
        found.append("LogConfig missing")
    drop = host.get("CapDrop") or []
    if "ALL" not in drop and not all(c in drop for c in CAP_DROP):
        found.append("CapDrop missing")
    return found
