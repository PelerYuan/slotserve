"""ContainerSpec -> Docker create body: the only place that knows the shape of Docker's JSON.

The body still has to go through ``harden()`` before it reaches Docker (``build.render_for_docker`` and
``helpers.helper_body`` do both); ``DockerClient.create`` refuses bodies that did not.
"""

from .model import ContainerSpec


def render(spec: ContainerSpec) -> dict:
    host: dict = {}
    if spec.devices is not None:
        host["DeviceRequests"] = [dict(d) for d in spec.devices]
    if spec.mounts:
        host["Mounts"] = [dict(m) for m in spec.mounts]
    if spec.binds:
        host["Binds"] = list(spec.binds)
    if spec.shm is not None:
        host["ShmSize"] = spec.shm
    if spec.init is not None:
        host["Init"] = spec.init
    if spec.restart is not None:
        host["RestartPolicy"] = {"Name": spec.restart}
    if spec.network:
        host["NetworkMode"] = spec.network
    if spec.exposed or spec.port_bindings:
        host["PortBindings"] = {k: [dict(b) for b in v] for k, v in spec.port_bindings.items()}
    host.update(spec.cpu)
    if spec.memory:
        host["Memory"] = spec.memory
    if spec.pids:
        host["PidsLimit"] = spec.pids
    if spec.log is not None:
        host["LogConfig"] = spec.log
    host.update(spec.host_extra)
    body: dict = {"Image": spec.image}
    if spec.hostname:
        body["Hostname"] = spec.hostname
    if spec.env:
        body["Env"] = [f"{k}={v}" for k, v in spec.env.items()]
    if spec.exposed:
        body["ExposedPorts"] = {f"{p}/tcp": {} for p in spec.exposed}
    if spec.labels:
        body["Labels"] = dict(spec.labels)
    body["HostConfig"] = host
    if spec.entrypoint is not None:
        body["Entrypoint"] = list(spec.entrypoint)
    if spec.cmd is not None:
        body["Cmd"] = list(spec.cmd)
    if spec.user is not None:
        body["User"] = spec.user
    if spec.workdir is not None:
        body["WorkingDir"] = spec.workdir
    return body
