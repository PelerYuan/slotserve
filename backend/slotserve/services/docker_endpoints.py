"""Every Docker Engine API endpoint and request field SlotServe uses, in one table (A10).

The same table drives three things, so a new Docker call is one new row here and nothing else:

1. ``tests/docker_mock.py``: the fake Docker answers 400 to any endpoint, query parameter or body field that is
   not declared here (fail-closed), so a test can no longer pass with a request real Docker would treat differently.
2. ``tests/contract/``: every endpoint must have at least one contract case that runs against the fake and against
   a real Docker daemon (CI job ``contract-docker``).
3. Later the node agent's Docker guard (``agent/docker_guard.py``, cluster branch): the same rows become its
   allowlist; everything else is refused with 403 (and ``validate`` gets the node's policy as well).

Paths are relative to the API version prefix (``/v1.41``) and already URL-decoded. A body field is a dotted path:
``HostConfig.Mounts[].Source`` is the ``Source`` of every element of ``HostConfig.Mounts``; ``*`` stands for any
key of a map (``HostConfig.PortBindings.*[].HostPort``). A declared field without declared sub-fields is opaque
(its value is not looked into: ``Labels``, ``Env``, ``ExposedPorts``).

Pure data and functions: no I/O, no imports from the rest of SlotServe.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

API_PREFIX = "/v1.41"
LABEL_PREFIX = "slotserve."

BodyKind = Literal["none", "json", "tar"]


@dataclass(frozen=True)
class Endpoint:
    name: str  # stable id used by contract tests (``containers.create``)
    method: str
    path: str  # ``/containers/{id}/start``; ``{ref}`` (an image reference) may contain slashes
    query: frozenset[str] = frozenset()  # allowed query parameters
    body: BodyKind = "none"
    fields: frozenset[str] = frozenset()  # allowed body fields (dotted paths, see the module docstring)
    validate: Callable[[Any], list[str]] | None = None  # extra rules on the body; returns problems
    # Only for objects carrying a ``slotserve.*`` label (enforced by the agent guard, which can look them up;
    # the fake only declares it).
    label_scoped: bool = False
    writes: bool = True  # changes state (the guard may allow read-only endpoints more widely)
    _regex: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        pattern = re.sub(r"\{ref\}", r"(?P<ref>.+)", self.path)
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        object.__setattr__(self, "_regex", re.compile(f"^{pattern}$"))

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method.upper() != self.method:
            return None
        m = self._regex.match(path)
        return m.groupdict() if m else None


# ---- rules for container create bodies ------------------------------------------------------------------------

HOST_NAMESPACE_FIELDS = ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode")


def _bind_source(bind: str) -> str:
    return bind.split(":", 1)[0]


def check_create(body: Any) -> list[str]:
    """What no SlotServe container may ask for, whatever node it runs on (the subset of ``harden`` that a
    request alone shows): privileged mode, the host's namespaces, the host's root directory, no SlotServe label."""
    if not isinstance(body, dict):
        return ["body: not an object"]
    problems = []
    host = body.get("HostConfig") or {}
    if not isinstance(host, dict):
        return ["HostConfig: not an object"]
    if host.get("Privileged"):
        problems.append("HostConfig.Privileged: not allowed")
    for key in HOST_NAMESPACE_FIELDS:
        if str(host.get(key) or "").lower() == "host":
            problems.append(f"HostConfig.{key}: host is not allowed")
    for bind in host.get("Binds") or []:
        if isinstance(bind, str) and _bind_source(bind).rstrip("/") == "":
            problems.append(f"HostConfig.Binds: the host's root directory ({bind}) is not allowed")
    for mount in host.get("Mounts") or []:
        if isinstance(mount, dict) and mount.get("Type") == "bind" and str(mount.get("Source", "")).rstrip("/") == "":
            problems.append("HostConfig.Mounts: the host's root directory is not allowed")
    labels = body.get("Labels") or {}
    if not isinstance(labels, dict) or not any(str(k).startswith(LABEL_PREFIX) for k in labels):
        problems.append(f"Labels: a {LABEL_PREFIX}* label is required")
    return problems


def _labelled(body: Any) -> list[str]:
    labels = (body or {}).get("Labels") if isinstance(body, dict) else None
    if not isinstance(labels, dict) or not any(str(k).startswith(LABEL_PREFIX) for k in labels):
        return [f"Labels: a {LABEL_PREFIX}* label is required"]
    return []


# ---- the table ---------------------------------------------------------------------------------------------------

_MOUNT = ("Type", "Source", "Target", "ReadOnly")
_DEVICE_REQUEST = ("Driver", "Count", "DeviceIDs", "Capabilities")
# Resource limits shared by create and ``docker update`` (services/devenv_cpu.py).
_LIMITS = ("CpusetCpus", "NanoCpus", "CpuShares", "Memory", "MemorySwap", "PidsLimit")

CREATE_FIELDS = frozenset(
    {
        "Image",
        "Hostname",
        "Env",
        "Cmd",
        "Entrypoint",
        "User",
        "WorkingDir",
        "ExposedPorts",
        "Labels",
        "HostConfig",
        "HostConfig.Binds",
        "HostConfig.Mounts",
        *(f"HostConfig.Mounts[].{k}" for k in _MOUNT),
        "HostConfig.DeviceRequests",
        *(f"HostConfig.DeviceRequests[].{k}" for k in _DEVICE_REQUEST),
        "HostConfig.ShmSize",
        "HostConfig.Init",
        "HostConfig.RestartPolicy",
        "HostConfig.RestartPolicy.Name",
        "HostConfig.NetworkMode",
        "HostConfig.ExtraHosts",  # slotserve-gw (containers/contributors/platform_env.py)
        "HostConfig.PortBindings",
        "HostConfig.PortBindings.*",
        "HostConfig.PortBindings.*[].HostIp",
        "HostConfig.PortBindings.*[].HostPort",
        *(f"HostConfig.{k}" for k in _LIMITS),
        # hardening (forwarders today; every container once containers/harden.py lands, R1-05)
        "HostConfig.ReadonlyRootfs",
        "HostConfig.CapDrop",
        "HostConfig.SecurityOpt",
        "HostConfig.Ulimits",
        "HostConfig.Ulimits[].Name",
        "HostConfig.Ulimits[].Soft",
        "HostConfig.Ulimits[].Hard",
        "HostConfig.LogConfig",
        "HostConfig.LogConfig.Type",
        "HostConfig.LogConfig.Config",
    }
)

ENDPOINTS: tuple[Endpoint, ...] = (
    # system
    Endpoint("system.version", "GET", "/version", writes=False),
    Endpoint("system.info", "GET", "/info", writes=False),
    # images
    Endpoint("images.inspect", "GET", "/images/{ref}/json", writes=False),
    Endpoint("images.pull", "POST", "/images/create", query=frozenset({"fromImage", "tag"})),
    Endpoint("images.remove", "DELETE", "/images/{ref}"),
    Endpoint(
        "images.commit",
        "POST",
        "/commit",
        query=frozenset({"container", "repo", "tag", "comment", "pause"}),
        # the image's own environment: SlotServe's variables emptied (containers/image_clean.py, R1-11)
        body="json",
        fields=frozenset({"Env"}),
        label_scoped=True,
    ),
    # containers
    Endpoint(
        "containers.create",
        "POST",
        "/containers/create",
        query=frozenset({"name"}),
        body="json",
        fields=CREATE_FIELDS,
        validate=check_create,
        label_scoped=True,
    ),
    Endpoint("containers.list", "GET", "/containers/json", query=frozenset({"all", "filters"}), writes=False),
    Endpoint("containers.inspect", "GET", "/containers/{id}/json", query=frozenset({"size"}), writes=False),
    Endpoint(
        "containers.archive_put",
        "PUT",
        "/containers/{id}/archive",
        query=frozenset({"path", "copyUIDGID"}),
        body="tar",
        label_scoped=True,
    ),
    # seeding new storage folders from an image without running it (containers/helpers.py, R1-06)
    Endpoint(
        "containers.archive_get",
        "GET",
        "/containers/{id}/archive",
        query=frozenset({"path"}),
        label_scoped=True,
        writes=False,
    ),
    Endpoint("containers.start", "POST", "/containers/{id}/start", label_scoped=True),
    Endpoint("containers.stop", "POST", "/containers/{id}/stop", query=frozenset({"t"}), label_scoped=True),
    Endpoint("containers.remove", "DELETE", "/containers/{id}", query=frozenset({"force"}), label_scoped=True),
    Endpoint("containers.wait", "POST", "/containers/{id}/wait", label_scoped=True),
    Endpoint(
        "containers.logs",
        "GET",
        "/containers/{id}/logs",
        query=frozenset({"stdout", "stderr", "tail", "timestamps"}),
        label_scoped=True,
        writes=False,
    ),
    Endpoint(
        "containers.stats",
        "GET",
        "/containers/{id}/stats",
        query=frozenset({"stream", "one-shot"}),
        label_scoped=True,
        writes=False,
    ),
    Endpoint(
        "containers.update",
        "POST",
        "/containers/{id}/update",
        body="json",
        fields=frozenset(_LIMITS),
        label_scoped=True,
    ),
    Endpoint(
        "containers.exec",
        "POST",
        "/containers/{id}/exec",
        body="json",
        fields=frozenset({"Cmd", "User", "AttachStdin", "AttachStdout", "AttachStderr"}),
        label_scoped=True,
    ),
    Endpoint("exec.start", "POST", "/exec/{id}/start", body="json", fields=frozenset({"Detach", "Tty"})),
    Endpoint("exec.inspect", "GET", "/exec/{id}/json", writes=False),
    # volumes
    Endpoint(
        "volumes.create",
        "POST",
        "/volumes/create",
        body="json",
        fields=frozenset({"Name", "Driver", "DriverOpts", "Labels"}),
        validate=_labelled,
        label_scoped=True,
    ),
    Endpoint("volumes.list", "GET", "/volumes", query=frozenset({"filters"}), writes=False),
    Endpoint("volumes.inspect", "GET", "/volumes/{name}", writes=False),
    Endpoint("volumes.remove", "DELETE", "/volumes/{name}", label_scoped=True),
    # networks
    Endpoint(
        "networks.create",
        "POST",
        "/networks/create",
        body="json",
        fields=frozenset({"Name", "Driver", "CheckDuplicate", "Labels"}),
        validate=_labelled,
        label_scoped=True,
    ),
    Endpoint("networks.list", "GET", "/networks", query=frozenset({"filters"}), writes=False),
    # the default bridge's address: where the container-side gateway listens (gateway/container_gw.py)
    Endpoint("networks.inspect", "GET", "/networks/{network}", writes=False),
    Endpoint(
        "networks.connect",
        "POST",
        "/networks/{network}/connect",
        body="json",
        fields=frozenset({"Container", "EndpointConfig", "EndpointConfig.Aliases"}),
        label_scoped=True,
    ),
    Endpoint(
        "networks.disconnect",
        "POST",
        "/networks/{network}/disconnect",
        body="json",
        fields=frozenset({"Container", "Force"}),
        label_scoped=True,
    ),
    Endpoint("networks.remove", "DELETE", "/networks/{network}", label_scoped=True),
)

BY_NAME: dict[str, Endpoint] = {e.name: e for e in ENDPOINTS}


def strip_prefix(path: str) -> str:
    """``/v1.41/containers/x/json`` -> ``/containers/x/json``."""
    return path[len(API_PREFIX) :] if path.startswith(API_PREFIX + "/") else path


def find(method: str, path: str) -> tuple[Endpoint, dict[str, str]] | None:
    """The endpoint a request is for (path with or without the version prefix), and its path parameters."""
    path = strip_prefix(path)
    for endpoint in ENDPOINTS:
        params = endpoint.match(method, path)
        if params is not None:
            return endpoint, params
    return None


def undeclared_fields(value: Any, fields: Iterable[str], prefix: str = "") -> list[str]:
    """Dotted paths in ``value`` that ``fields`` does not declare (see the module docstring)."""
    declared = frozenset(fields)
    out: list[str] = []

    def has_children(path: str) -> bool:
        return any(f.startswith(path + ".") or f.startswith(path + "[]") for f in declared)

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if child_path not in declared:
                    wildcard = f"{path}.*" if path else "*"
                    if wildcard not in declared:
                        out.append(child_path)
                        continue
                    child_path = wildcard
                if has_children(child_path):
                    walk(child, child_path)
        elif isinstance(node, list) and path:
            for item in node:
                if isinstance(item, (Mapping, list)):
                    walk(item, path + "[]")

    walk(value, prefix)
    return out


def check_request(method: str, path: str, query: Iterable[str], body: Any) -> tuple[Endpoint | None, list[str]]:
    """(the endpoint, problems) for one request; ``body`` is the decoded JSON body, raw bytes or None.

    Problems: an undeclared endpoint, query parameter or body field, a body where none is expected, and whatever
    the endpoint's ``validate`` finds. An empty list means the request is one SlotServe is known to make."""
    found = find(method, path)
    if found is None:
        return None, [f"{method.upper()} {strip_prefix(path)}: not a declared Docker endpoint"]
    endpoint, _ = found
    problems = [f"query parameter {q!r} is not declared" for q in sorted(set(query) - endpoint.query)]
    if endpoint.body == "json":
        if not isinstance(body, dict):
            problems.append("a JSON object body is required")
        else:
            problems += [f"body field {f!r} is not declared" for f in undeclared_fields(body, endpoint.fields)]
            if endpoint.validate is not None:
                problems += endpoint.validate(body)
    elif endpoint.body == "none" and body not in (None, b"", {}, ""):
        problems.append("no body is expected")
    return endpoint, problems
